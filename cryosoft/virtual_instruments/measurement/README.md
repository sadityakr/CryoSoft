# virtual_instruments/measurement/

## Purpose
Virtual instruments for electrical transport measurements. Every class here is a
**measurement method**: a self-describing measurement VI that declares its own
GUI knobs and output shape and implements one uniform lifecycle, so a generic
procedure can run any of them without knowing which instrument or protocol is
behind it. The standard is defined and documented on
`MeasurementInstrumentBase` in `virtual_instruments/base.py`.

## Architecture layer
L1 — Virtual Instruments.

## Entry (what comes in)
Driver dicts and `init_params` (the setup constants each class needs).
- `DCSeparateMeasurementVI`: `{"source": <K6221>, "meter": <K2182A>}`,
  `init_params: {max_source_current_A}` (the setup's excitation ceiling).

## Exit (what goes out)
The measurement-method standard (all classes obey it):

Self-description (class attributes)
- `measurement_parameters: dict[str, ParamSpec]` — the VI's GUI-facing knobs,
  the single owner of those specs.
- `measurement_data_keys: list[str]` — the raw-sample array names
  `take_reading()` returns, each ending `_array` (e.g. `"voltage_V_array"`).
- `measurement_scalar_columns: dict[str, str]` — per-point scalar columns
  (name → "float"/"int"). The **mean/error/array convention**: for every
  quantity behind a `"{quantity}_array"` key, this carries `"{quantity}"`
  (the mean — what the GUI plots) and `"{quantity}_error"` (the standard
  error of the mean), both dtype "float"; plus optional VI-specific extras
  unrelated to any array, e.g. `n_valid`. Build both attributes with
  `MeasurementInstrumentBase.quantity_columns(*names)` rather than
  hand-writing the suffixes — see the pattern in
  `dc_separate_measurement.py`.

Uniform lifecycle (methods)
- `data_arrays(params) → {array_name: length}` — output shape (the `_array`
  keys) for the same `params` `initiate_measurement()` will receive, computed
  before arming the hardware.
- `initiate_measurement(**params) → None` — arm/configure the hardware. Accepts
  the `measurement_parameters` keys, all defaulted. `@control(panel=False)`-
  decorated where the VI exposes arming to the GUI (front panel only, never the
  compact card). Deliberately NOT named `initiate`: the plain lifecycle
  `initiate()` on a measurement VI is a harmless connection check (pings the
  drivers, raises `CryoSoftCommunicationError` when unreachable), so a bulk
  Initiate-All can never start a source current.
- `take_reading() → dict` — take ONE datapoint. No arguments. For every
  quantity, returns the mean/error/array triple: the raw-sample array
  (`"{quantity}_array"`, NaN-padded to the length `data_arrays` declared),
  the mean (`"{quantity}"`), and the standard error of the mean
  (`"{quantity}_error"`) — computed over the VALID samples with
  `self.mean_and_sem(...)`. Also returns every other
  `measurement_scalar_columns` key (e.g. `n_valid`, see below). A VI whose
  instrument can return fewer points pads the array with `float("nan")` and
  computes the mean/error/`n_valid` over the samples actually delivered,
  BEFORE that padding is applied. This fixed-shape guarantee prevents
  HDF5 layout mismatches mid-run.
- `standby() → None` — safe-off idle state.
- `ping() → bool` — IDN check on all drivers.
- `read_now() → None` (optional) — the bench hook: one `take_reading()` at
  the excitation already armed, cached into `@monitored` fields so an
  operator can confirm the settings produce sane readings before committing
  to a run. It is the natural home of a `read`-class capability
  (`@control(panel=False, action_class="read")` — GLOSSARY.md's **Action
  class**), since it commands nothing new; `dc_separate_measurement.py` is
  the shipped example.
- `reading_setters: dict[str, str]` — OPTIONAL reading-loop declaration
  (default `{}`): maps a `measurement_parameters` name to the cheap setter
  method that reprograms just that quantity between readings without
  re-arming (e.g. `{"current_A": "set_source_current"}`). One entry is all a
  VI declares — the generic sweep procedure offers the parameter in its
  Reading loop slots, dispatches the setter before each value's reading, and
  every measurement column carries a real `(n_loop1, n_loop2)` array axis in
  HDF5 (never suffixed names) — axis index → value lives in the run's
  metadata as `procedure_params["loop1_values"]` / `["loop2_values"]`.
  Setters must accept the parameter under its own name and never change the
  reading's shape. The same standard lives on `BaseVirtualInstrument` (plus
  `reading_parameters` / `reading_safe_off`), so non-measurement VIs like
  the switch participate identically. Full contract in the base docstrings.

## Interface contract
DC measurement classes inherit `DCMeasurementBase` (which fixes the DC-resistance
shape: `readings_per_point` samples of `voltage_V` and `current_A`). Other
methods inherit `MeasurementInstrumentBase` directly. Both bases live in
`virtual_instruments/base.py`; `MeasurementInstrumentBase` carries the full
written standard in its docstring. `tests/test_conformance.py` enforces the
standard (declaration validity, lifecycle presence, and a sim round-trip) for
every measurement VI automatically.

## Raw diagnostic blocks
Some instruments return far more raw data per reading than any single
physical quantity — e.g. an engine reporting dozens of raw channels
(voltages, ranges, setpoints, lock quality, …) alongside the one or two the
operator actually derives a result from. The mean/error/array convention
above cannot express this: it hard-requires every `_array` key to pair with
a same-quantity mean/error, which makes no sense for a block mixing many
different physical units with no single mean to report. A VI with this need
declares a SECOND, orthogonal self-description instead of forcing the block
through the convention above:

- `measurement_raw_blocks: ClassVar[dict[str, list[str]]]` — block name →
  ordered channel-label list, fixing the block's channel axis. Empty (the
  default) means no raw block.
- `raw_block_row_counts(params) -> dict[str, int]` — declared block name →
  row count for the same `params` `initiate_measurement()` will receive,
  mirroring `data_arrays()`'s per-instance, params-dependent role. Base
  returns `{}`.
- `take_reading()` returns a block's value under its block-name key as a
  nested `rows x channels` list — row order matching the declared row
  count, column order matching the declared label list. An instrument that
  under-delivers rows pads the ROW axis with `float("nan")` the same way an
  array quantity does; the channel axis is fixed and never padded.

A block's own `rows x channels` matrix is deliberately excluded from
`measurement_data_keys`/`measurement_scalar_columns` — it never itself
appears in the GUI's plot-axis dropdowns, exactly like an `_array` column
today. Its declared channels are independently plottable, though:
`SweepMeasureProcedure` automatically derives one scalar column per
channel (row axis reduced by a NaN-safe mean — the same "N readings at one
measurement point" treatment `mean_and_sem` gives a quantity's own array
column), merged
into `DataSchema.measurement_scalars` and `live_plot_measurement_keys()`
with zero VI-side code (`_raw_block_channel_columns`/`measure()` in
`procedure.py`) — a `CryoSoftConfigError` at `initiate()` if a channel
label collides with a sweep/scalar column or another block's channel.
Unlike every other
measurement column, a block does NOT always carry the reading loop's real
`(n_loop1, n_loop2)` **loop axis**: `DataSchema.measurement_blocks` only
adds that axis when a reading loop is actually configured for the run
(`loop_shape != (1, 1)`); with no reading loop its HDF5 shape stays bare
`(rows, cols)` per sweep point, not `(1, 1, rows, cols)`. This is handled
entirely at the procedure/schema layer (`SweepMeasureProcedure.measure()`
squeezes the trivial axis away before saving) — a VI's own `take_reading()`
always returns the block as a flat `rows x channels` list regardless of any
loop. The shape the mechanism is built for is an instrument whose every
reading is a wide record — a many-column diagnostic dump, or a camera frame
whose rows and columns are pixels — where the block IS the datum and the
scalar columns are derived from it.

The declared `measurement_raw_blocks` label list is not just an in-memory
contract — `DataManager` writes it to disk as the block dataset's own
`channel_names` HDF5 attribute (column index → channel name), alongside an
`axes` attribute naming every dimension in order. A reader opening the file
directly (h5py, HDFView) sees both attached to `/data/<block_name>` itself,
with no need to parse the `/metadata` group's JSON `data_config` blob.

## Image blocks: a frame is a 2D block, not a row of channels

The **image-block standard** is the raw block's sibling for a camera or any
instrument whose reading is a frame. A frame has a raw block's `(rows, cols)`
storage shape, but no channel per column: every element is one pixel in one
unit, and no per-column scalar makes sense. A VI declares it with its pixel
dimensions and unit instead of a channel-label list:

```python
from cryosoft.core.plan import ImageBlock

measurement_image_blocks: ClassVar[dict[str, ImageBlock]] = {
    "frame": ImageBlock(height_px=256, width_px=256, unit="counts",
                        description="Widefield frame at the sample"),
}
```

- `measurement_image_blocks: ClassVar[dict[str, ImageBlock]]` — block name →
  declaration. The declared `height_px`/`width_px` fix the frame's shape for
  every reading (`raw_block_row_counts()` plays no part). Empty (the default)
  means the VI takes no frames.
- `take_reading()` returns the frame under the block name as a
  `(height_px, width_px)` numpy array (or an equivalently nested list),
  alongside the scalars and arrays it already returns.

Storage and reading reuse the raw block's path with one difference in what
the dataset says about itself: `SweepMeasureProcedure` puts the block into
`DataSchema.measurement_blocks` with its declared shape (same loop-axis rule
— bare `(rows, cols)` with no reading loop, `(n_loop1, n_loop2, rows, cols)`
with one), and `DataManager` writes `block_kind = "image"`, `unit` and
`description` attributes with `axes` ending in `row, col` and NO
`channel_names`. `data_reader` reports the column with role `image` (from the
`data_config`'s `measurement_image_blocks` section, or from the dataset's own
`block_kind` when the declaration is missing) and serves one frame through
`read_image(name, index, loop1=0, loop2=0)`, on a file and on the live
`RunBuffer` alike. No scalar column is ever derived from a frame: the live
plot and the generic sweep recipe use the VI's scalar columns
(`measurement_scalar_columns` — a mean intensity, say), the image-stack recipe
reads the frames. Conformance requires each declared block's
`height_px`/`width_px` to match what the sim's `take_reading()` actually
returns, and that no image block name collides with another column.

## Under-delivery: the n_valid standard
A VI whose `take_reading()` can deliver fewer raw samples than `data_arrays()`
declared for a quantity MUST report an `n_valid` scalar column (dtype `"int"`,
added to `measurement_scalar_columns`) — the number of samples the mean and
SEM were actually computed over. The returned array preserves the delivered
rows verbatim (including any instrument-emitted NaN, e.g. a ratiometric
divide-by-zero), padded with `float("nan")` out to the declared length. The
mean, SEM, and `n_valid` are computed over the delivered samples BEFORE
padding — never by filtering NaN out of the padded array, which would
conflate CryoSoft's own padding with a NaN the instrument itself emitted. Row
selection when more samples arrived than requested (first-*n* vs last-*n*) is
per-instrument physics — documented on that VI's own `take_reading()`, not
standardised here. A VI whose instrument always delivers exactly the
requested sample count has nothing to report and may omit `n_valid`
entirely. Full text: `MeasurementInstrumentBase`'s docstring, "Under-delivery:
the `n_valid` standard".

## Externally configured instruments
Some instruments expose far more configuration surface than a VI wraps
(analysis modes, pulse trains, reference muxing, preamp modes, …). A VI may
support an operator configuring the instrument with the vendor's own tool and
letting CryoSoft run only the measurement — arm the data path, trigger,
read, and save a fixed-shape data block — without touching that
configuration. This is the `configured_externally` standard on
`MeasurementInstrumentBase`, motivated by single-client instrument firmware
where the vendor tool and CryoSoft are mutually exclusive at the instrument.
No shipped VI declares it today; the standard is stated here so the first VI
that needs it inherits the whole mechanism rather than inventing one.

`MeasurementInstrumentBase.__init__` reads the optional init param
`configured_externally: bool` (default `False`) from `init_params` and
stores it as `self._configured_externally` — config-driven, per-VI, via
`devices.yaml`'s `init_params`, with zero per-subclass boilerplate. Omitted
or `False` leaves every existing VI's behavior unchanged.

When `self._configured_externally` is true:
- `initiate_measurement()` MUST NOT write any excitation, analysis, or
  routing parameter to the instrument — the external tool owns them. It
  MUST still: (a) verify connectivity with a TRUE ROUND TRIP — a query that
  fails loudly on a dead or externally-held channel; (b) arm the data path
  (buffers, channel/format selection, anything `take_reading()`'s decode
  depends on); (c) read back from the instrument any value its own timing
  or decoding depends on; and (d) set the internal state `take_reading()` /
  `data_arrays()` require. Inert `measurement_parameters` MUST still be
  accepted (procedures pass them regardless of mode); log one INFO listing
  the ignored parameters.
- `standby()` MUST NOT overwrite externally-owned source state. It resets
  only CryoSoft's own internal arming state; releasing the hardware resource
  is no longer this method's own job — see "Detached-idle lifecycle" below.

A VI supporting external configuration MUST declare
`externally_owned_parameters: ClassVar[frozenset[str]]` — the
`measurement_parameters` names the external tool owns in that mode
(excitation/analysis/routing). Declare-and-derive: the procedure form
(`active_measurement_parameters`) and the reading-loop registry
(`reading_parameters`) both hide those names automatically once
`configured_externally` is true, with no per-VI form code. Data-path
parameters (e.g. a tensor-component selector, a readings-per-point count —
anything that writes nothing to the instrument) stay off this set, so they
remain operator-controlled, and rendered, in every mode. The empty default
changes nothing for a VI that does not support external configuration;
`tests/test_conformance.py` checks every declared name is a real
`measurement_parameters` key.

A VI that captures a provenance snapshot at arming time SHOULD expose it as
`self.last_settings_snapshot` (a plain `dict`); the sweep procedure
(`SweepMeasureProcedure.measure()`) duck-types this attribute and records it
into the run's HDF5 `/metadata` automatically, once per run, via
`DataManager.record_settings_snapshot()` — no per-VI plumbing required.

**Detached-idle lifecycle**: an externally configured VI is the motivating
case for `BaseVirtualInstrument`'s declared `detach_when_idle` standard
(full text there — "Detach-when-idle declaration" — and in GLOSSARY.md's
**Availability**); this section states only how the two fit together, never
restates the standard itself. A VI opts in with one line —
`detach_when_idle` returning `self._configured_externally` — and the base
does everything else: born detached (`__init__` calls `self._detach()`
before returning, so starting CryoSoft while the vendor tool is open builds
cleanly), a `ping()` verify-and-release path (reattach, a true round trip,
then release — so `initiate()`, which calls `ping()`, returns a clean
failure verdict rather than raising when the instrument is currently held
by the external tool), `initiate_measurement()` reacquiring the connection
(via `self._attach()`) for the measurement window, and `standby()`'s
`__init_subclass__`-wrapped release handing the session back the instant
the VI's own safe-off commands return — every run path already ends in
`standby()`, so the instrument frees itself automatically and the operator
may attach the vendor tool at any time between runs. No VI-specific
`ping()` override or release branch in `standby()` is needed to get this.
Never reconnect opportunistically in the background (e.g. from a monitored
poll): a wrongly-timed connect can fail silently against the external
tool's session, not loudly. Full text: `MeasurementInstrumentBase`'s
docstring, "Externally configured instruments".

## Shared-instrument mode discipline
Two measurement methods can be wired to the SAME physical driver instance
(two VIs both naming the one `keithley_6221` entry in `devices.yaml`'s
`real_drivers`), because only one measurement VI is armed at a time — but the
underlying instrument can have more than one mutually exclusive SCPI/operating
mode (plain DC output vs. the bipolar delta engine). A driver method that
establishes one of these modes MUST be **idempotent and self-recovering**: it
must reassert its own required mode unconditionally, never assume the
instrument is already in a compatible state left over from whichever VI ran
last. This is the primary defense (see `SimKeithley6221.set_current()`'s
unconditional return to fixed DC output, mirroring how its delta programming
always leads with an abort).
`stop_delta_mode()`-style teardown methods should still also return the
instrument to a documented idle baseline as defense-in-depth (useful for a
human inspecting the instrument between runs), but a VI's
`initiate_measurement()` must
never *rely* on a previous VI's `standby()` having been called correctly. A
sim driver modeling more than one such mode should track it (e.g.
`SimKeithley6221._mode`) so a VI that skips the defensive reassertion fails in
tests, not on hardware — see `tests/test_l1_virtual_instruments.py`'s
shared-6221 handoff test for the pattern.

## How to add a new measurement VI
1. Subclass `DCMeasurementBase` (for a DC-resistance method) or
   `MeasurementInstrumentBase` (any other protocol).
2. Declare `measurement_parameters` (ParamSpecs) and derive
   `measurement_data_keys` / `measurement_scalar_columns` from
   `MeasurementInstrumentBase.quantity_columns(*names)` for each quantity the
   VI measures — plus `n_valid` (dtype `"int"`) if the instrument can return
   fewer readings than requested (MUST, per the "Under-delivery: the
   `n_valid` standard" section above).
3. Implement `data_arrays(params)`, `initiate_measurement(**params)`,
   `take_reading()`, `standby()` (and `ping()`). Keep `@control(panel=False)` on
   `initiate_measurement()` if the GUI should
   be able to arm it. In `take_reading()`, pad short returns to the declared
   length with `float("nan")`, then call `self.mean_and_sem(valid_samples)`
   per quantity to fill in the mean/error and report the true count in a
   scalar column if applicable. Declare a `reading_setters` entry (parameter →
   setter method) for any parameter the reading loop should be able to vary
   per point (see the Exit section above).
4. If the VI needs a driver role not already in
   `tests/test_conformance.py::_SIM_MEASUREMENT_DRIVER_CLASSES`, add its sim
   driver there so the round-trip conformance test can build it.
5. Register in `devices.yaml`; add behaviour tests to `tests/test_l1_new_vis.py`.

## Files
- `dc_separate_measurement.py` — `DCSeparateMeasurementVI`: Keithley 6221 source +
  2182A nanovoltmeter, simple DC mode. Declares the reference `reading_setters`
  entry `{"current_A": "set_source_current"}`, so the reading loop can measure
  a user-entered current list (e.g. `1e-6, -1e-6`) at every sweep point
  (per-slot index-label columns); the setter reprograms the source in place
  with no re-arm cost. Also the shipped example of a `read`-class capability:
  `read_now()` plus the `last_voltage_V` / `last_n_valid` fields it fills.
  tests: `tests/test_measurement_dc_vi.py`,
  `tests/test_l1_new_vis.py` (`TestDCSeparateMeasurementVI`),
  `tests/test_new_procedures.py` (reading loop).
- `__init__.py` — package marker. tests: none.
