# Make raw diagnostic block channels selectable in the procedure trend plots

**ARCHIVED — IMPLEMENTED 2026-07-28.**

**Status:** proposal, no code yet.
**Scope:** let every channel of a measurement VI's raw diagnostic block
(today: `TensormeterRTM2MeasurementVI`'s 44-column `raw_channels_block`)
appear as its own selectable Y-axis in `ProcedureWindow`'s live trend plots,
without changing the raw block's existing HDF5 shape or the reading-loop
(loop1/loop2) convention it must keep supporting.
**Date:** 2026-07-28

---

## Context

`docs/plans/archive/tensormeter-raw-channel-capture.md` added the "raw
diagnostic block" convention: a measurement VI declares
`measurement_raw_blocks` (block name → ordered 44-channel label list) and
`raw_block_row_counts()`, and `SweepMeasureProcedure`/`DataManager` save the
full block to HDF5 as a real `(rows, cols)` grid per sweep point — or
`(n_loop1, n_loop2, rows, cols)` when a reading loop is configured
(`DataSchema.measurement_blocks`). That plan's own Verification step 4
explicitly signs off on the block being excluded from the live plot's
axis dropdowns, "the same way `*_array` columns already are" — and
`MeasurementInstrumentBase`'s "Raw diagnostic blocks" docstring
(`cryosoft/virtual_instruments/base.py:1008-1011`) states this as settled
behaviour: "A raw block is a diagnostic/provenance record, not a plotted
quantity... it never appears in the GUI's plot axis dropdowns."

That was correct for the original ask (preserve the data), but the operator
now wants to actually watch individual RTM2 channels (e.g.
`res_a_dc_ohm`, `lockin_frequency_Hz`) update live on the Procedure
window's trend plots, the same way `res_a_ohm`/`voltage_V` already do. Two
things stand in the way, both in the GUI/procedure layer, not storage:

1. `SweepMeasureProcedure.live_plot_measurement_keys()`
   (`cryosoft/core/procedure.py:1179-1204`) builds its key list from
   `vi.measurement_data_keys` and `vi.measurement_scalar_columns` only — it
   never looks at `vi.measurement_raw_blocks`, so no raw-block channel can
   ever reach the X/Y selector combo boxes.
2. Even if a key list included the block name, `LivePlotPanel._lookup()`
   (`cryosoft/gui/live_plot_panel.py:298-308`) does `raw[i1][i2]` and expects
   a scalar back. A raw block's `(i1, i2)` cell is a `rows × 44` matrix —
   not a scalar — so indexing into it can never produce one plottable
   number without first picking both a row and a channel.

The first, rejected approach (see prior discussion in this session) was to
change the raw block's own HDF5 layout to a flat `(total_rows, 44)` table so
any column trivially becomes a plain series. That was rejected: the
existing `(n_loop1, n_loop2, rows, cols)` shape is deliberately kept for
future delta-mode/DC-mode work that drives `loop1`/`loop2` through the
reading loop, and flattening the block would destroy that axis structure
for every future user of raw blocks, not just this one plotting need.

## Approach

Do not touch the block's storage shape or the reading-loop convention.
Instead, treat each raw block channel the way `res_a_ohm` already treats
its own raw sample array: reduce the row axis to one scalar per
`(loop1, loop2)` cell (NaN-safe mean), and expose that reduction as an
ordinary `measurement_scalars` column — reusing 100% of the existing
scalar-column machinery (`DataSchema`, `DataManager`, `LivePlotPanel`'s
Loop 1 / Loop 2 selectors) with zero changes to any of them. The block
itself keeps saving in full, unchanged, so nothing here forecloses a
future delta/DC-mode use of `loop1`/`loop2` against the raw block.

**Why a mean, not the raw rows.** A raw block's row axis
(`raw_block_row_counts()`) is `readings_per_point` — repeated
demodulation-window samples at the *same* `(loop1, loop2)` cell, exactly
the axis `res_a_ohm_array` already carries for `res_a_ohm`. This is not a
loop1/loop2 concept: it is the "N readings at one measurement point"
count also seen as `n_readings` in `measurement_delta_mode.py`'s
`take_reading()` (`measurement_delta_mode.py:285-330`), which reduces the
same way via `mean_and_sem()` into `voltage_V`/`current_A` — no
selection, no loop involvement, just a repeated-sample count at a single
point. Averaging the raw block's row axis is therefore not a new
information loss or a new convention; it is the same reduction every
plotted scalar in this VI (and `measurement_delta_mode.py`'s VI) already
undergoes, so a channel column lands at the same
one-row-per-`(sweep_point, loop1, loop2)` grain as every other plotted
column and lines up on the same X value. The full, unaveraged `rows × 44`
matrix remains on disk in `raw_channels_block` for anyone who needs
individual raw samples — this only adds a second, reduced view, it does
not remove the first. Genuine per-row plotting (each of the 5 demod
windows as its own point) would need the row axis to carry its own X-like
identity and is a materially different feature; out of scope here.

**Why the procedure layer, not the VI.** The reduction needs nothing VI-
specific — it only needs `vi.measurement_raw_blocks`' label lists and the
per-reading block matrix `take_reading()` already returns. Doing it once in
`SweepMeasureProcedure` means every current and future raw-block VI gets
plottable channels automatically, with zero per-VI code, matching
CLAUDE.md's standards-driven principle ("implementing the standard means
minimal new code and zero changes to the core").

### 1. `cryosoft/core/procedure.py`

- New `staticmethod _raw_block_channel_labels(vi)`: flatten
  `vi.measurement_raw_blocks.values()` into one ordered label list. Shared
  by the two call sites below so they can never drift apart.
- New `_raw_block_channel_columns(self, vi, sweep_columns)`: derive
  `{label: "float"}` for every raw-block channel, raising
  `CryoSoftConfigError` if a label collides with a sweep column, an
  existing `measurement_scalar_columns` entry, or another block's label —
  mirrors the existing parameter-collision check pattern already used a
  few lines above in `_build_procedure_param_groups`
  (`procedure.py:1158-1165`).
- `_build_data_schema()` (`procedure.py:1320-1355`): merge
  `_raw_block_channel_columns(vi, sweep_columns)` into the
  `measurement_scalars` dict passed to `DataSchema`. `DataSchema`/
  `DataManager` need no change — a channel column is indistinguishable
  from any other scalar column to both.
- `measure()` (`procedure.py:1444-1521`): add `_raw_block_channel_labels(vi)`
  to `keys` so `grids` allocates a `(n_loop1, n_loop2)` slot for each
  channel. Inside the reading loop, after `take_reading()`, for each block
  compute `_nanmean` per column over that reading's `rows × cols` matrix and
  store it at `grids[label][i1][i2]`. The existing no-loop squeeze (only
  `block_key`, never a channel label) is untouched — channel columns always
  keep their `(n_loop1, n_loop2)` grid like any other scalar, per the
  existing rule.
- New module-level `_nanmean(values)` helper (`math.isnan` filter +
  `statistics.fmean`), documented as the row-axis analogue of
  `MeasurementInstrumentBase.mean_and_sem` minus the SEM half (a diagnostic
  channel has no declared error column).
- `live_plot_measurement_keys()` (`procedure.py:1179-1204`): include
  `_raw_block_channel_labels(vi)` in the returned list (still filtered by
  the existing `not key.endswith("_array")` guard, which none of these
  names hit).

### 2. `cryosoft/virtual_instruments/base.py`

- Update the "Raw diagnostic blocks" docstring section
  (`base.py:1008-1011`): a block's own matrix is still never a plot key,
  but its declared channels now are, automatically, via
  `SweepMeasureProcedure`'s row-mean reduction — no VI-side change
  required. Cross-reference this document is **not** added (code cites
  standards, never plan documents, per CLAUDE.md's code-reference
  standard) — the docstring itself carries the updated rule.

### 3. `tensor_component`'s role narrows (docs only, no behavior change)

`tensor_component`'s `ParamSpec` description
(`tensormeter_rtm2_measurement.py:243-252`) and
`_warn_if_tensor_component_inconsistent()`'s docstring
(`tensormeter_rtm2_measurement.py:606-625`) currently frame the parameter
as choosing what the operator can *see*: "extracted... into the saved
res_a_ohm/res_b_ohm columns", checked against the externally-configured
snapshot because a wrong guess today means the only plottable columns show
the wrong physics. Once every raw channel is independently plottable, that
framing is stale — a wrong `tensor_component` guess no longer blocks
seeing correct data; the operator can just plot `res_a_1st_re_ohm` (etc.)
directly. `tensor_component` still has a real job: it is the only thing
that decides which component gets the statistically rigorous treatment
(`res_a_ohm`/`res_a_ohm_error`/`n_valid`, mean+SEM computed before NaN
padding per `MeasurementInstrumentBase`'s mean/error/array convention) used
for analysis and session export — the raw-channel columns added here are
a plain nanmean with no error bar. Update both docstrings to describe that
narrower "which component is analyzed", not "which component is
displayed" — no parameter removal, no behavior change, no test impact.

### 4. Standards documentation

- `cryosoft/virtual_instruments/measurement/README.md`: extend the "Raw
  diagnostic blocks" section with the plot-column behaviour.
- `GLOSSARY.md`: extend the "Raw diagnostic block" entry with the same.

### 5. Tests

- `tests/test_new_procedures.py`: extend the existing raw-block section
  (`test_raw_block_no_loop_axis_when_no_reading_loop`,
  `test_raw_block_carries_loop_axis_when_reading_loop_active`, around line
  1056) with:
  - A `live_plot_measurement_keys()` assertion (alongside
    `test_live_plot_keys_stay_plain_and_loop_labels_drive_the_selectors`,
    line 884) that every RTM2 raw-block label appears in the returned key
    list.
  - A `measure()` round trip using the existing `RTM2_FAST` /
    `_register_rtm2` / `_field_proc` fixtures asserting: (a) the saved HDF5
    gains 44 new scalar datasets, one per channel label, each shaped `(N,
    1, 1)`; (b) a channel's value equals the NaN-safe mean of the
    corresponding column across `raw_channels_block`'s row axis for that
    point; (c) `raw_channels_block` itself is unchanged in shape and
    values (the existing two tests already assert its shape — extend them
    rather than duplicate).
  - The loop-active variant (`_field_proc_scanner`/`ROUTES2`) asserting
    channel columns carry the real `(n_loop1, n_loop2)` grid same as
    `res_a_ohm` does, unlike the block itself which only gains that axis
    conditionally.
  - A collision test: a fake VI whose raw-block label collides with an
    existing scalar column name raises `CryoSoftConfigError` from
    `initiate()`.
- `tests/test_conformance.py`: no change expected — it walks declared
  class attributes, not the procedure-layer derived columns.

## Verification

1. Targeted: `pytest tests/test_new_procedures.py
   tests/test_tensormeter_measurement_vi.py -m "not hardware"`.
2. `make check` (ruff + lint-imports + full non-hardware suite) — confirms
   no layer-contract violation (this stays within L1 docstring + L4
   procedure, no new cross-layer import; `DataManager`/`plan.py` untouched).
3. Run the app against a config with `tensormeter_measurement`, start a
   short Field Sweep with `readings_per_point` small. Confirm: (a) the
   Procedure window's Y-axis dropdown now lists all 44 raw channel names
   alongside `res_a_ohm`/`res_b_ohm`; (b) picking one live-updates a curve
   against the field axis; (c) opening the resulting `.h5` shows both the
   unchanged `raw_channels_block` dataset and the 44 new scalar datasets
   agreeing with each other (channel value ≈ mean of the block's matching
   column for that point).
4. With a reading loop configured (e.g. a switch route), confirm the Loop 1
   / Loop 2 selectors work for a raw-block channel exactly as they do for
   `res_a_ohm` today.
