# Save all 44 RTM2 raw channels per reading, alongside the plotted scalar

**ARCHIVED — IMPLEMENTED 2026-07-28.**

**Status:** proposal, no code yet.
**Scope:** preserve every raw column the Tensormeter RTM2 driver returns per
demodulation window, in addition to the existing operator-selected
`res_a_ohm`/`res_b_ohm` scalar/array pair, via a new parallel "raw
diagnostic block" convention on `MeasurementInstrumentBase`.
**Date:** 2026-07-28

---

## Context

Investigating why the Procedure window's live plot showed nothing for a
Tensormeter run led to two findings: (1) the plot was fine — the operator
just needs `field_T` vs. `res_a_ohm`/`res_b_ohm`, since Tensormeter runs have
no voltage/current columns; (2) the RTM2 driver's `read_new_data()` returns
**44 raw columns** per demodulation window (`_DATA_COLUMNS` in
`cryosoft/drivers/tensormeter_rtm2.py:58-103` — voltages, currents,
impedance, all 14 Res A/Res B tensor components, switch status, ranges,
setpoints, lock quality, etc.), but
`TensormeterRTM2MeasurementVI.take_reading()`
(`cryosoft/virtual_instruments/measurement/tensormeter_rtm2_measurement.py:643-646`)
extracts only the 2 columns matching the operator's chosen `tensor_component`
and discards the other 42 every reading. That data is unrecoverable once the
run finishes.

The user wants every one of the 44 channels preserved for every reading,
while still being able to select one quantity (today's `res_a_ohm`/
`res_b_ohm`, driven by `tensor_component`) as the mean+error scalar shown
live and used for quick analysis. This requires a genuinely 2D per-point
array (readings × 44 channels), which the existing "mean/error/array"
convention (`quantity_columns()`,
`cryosoft/virtual_instruments/base.py:1211-1249`, enforced automatically by
`tests/test_conformance.py::test_measurement_vi_mean_error_array_convention`)
does not support — that convention hard-requires every `*_array` key to pair
with a same-quantity mean/error, which makes no sense for a block mixing 44
different physical units. So this plan introduces a small, parallel,
deliberately-scoped standard — "raw diagnostic blocks" — alongside the
existing convention, exactly as CLAUDE.md prescribes for a genuinely new
need: write it down, add a conformance test, touch nothing existing.

## Approach

Add a second, optional per-VI declaration for a fixed-shape 2D raw block
(rows × channels) that sits alongside `measurement_data_keys`/
`measurement_scalar_columns` without replacing them. `res_a_ohm`/
`res_b_ohm`/their errors/arrays and `tensor_component` stay exactly as they
are today (per the user's answers: keep the existing selector, unchanged).
One new field, `raw_channels_block`, carries all 44 columns for every
triggered reading.

### 1. New base-class convention (`cryosoft/virtual_instruments/base.py`)

In `MeasurementInstrumentBase`, add:
- A new docstring section ("Raw diagnostic blocks") next to the existing
  mean/error/array section, explaining when to use it (a VI needs to
  preserve the complete raw instrument row per reading, orthogonal to any
  single derived quantity) and how it differs (no mean/error pairing, fixed
  channel-label list, not excluded from HDF5 the way arrays would need
  companions).
- `measurement_raw_blocks: ClassVar[dict[str, list[str]]] = {}` — block name
  → ordered channel-label list (the label list length fixes the block's
  channel axis).
- `raw_block_row_counts(self, params: Mapping[str, Any]) -> dict[str, int]`
  — default `{}`; a VI with blocks overrides it to return `{block_name: rows}`
  per declared block (mirrors `data_arrays()`'s existing per-instance,
  params-dependent role).

### 2. Schema + storage plumbing (`cryosoft/core/plan.py`, `cryosoft/core/data_manager.py`)

- `DataSchema` (`plan.py:574-736`): add `measurement_blocks: dict[str,
  tuple[int, int]] = field(default_factory=dict)`. Add a
  `_validate_block_shapes()` helper mirroring `_validate_array_lengths`
  (`plan.py:532-549`) but validating 2-tuples of positive ints. Extend
  `__post_init__` to validate/copy it, and `validate()` to include block
  names in the declared/present key reconciliation and check each block's
  value against `(*loop_shape, *block_shape)` — reusing
  `_nested_shape_leaves()` (`plan.py:552-571`) unchanged, since it is already
  shape-length-agnostic.
- `DataManager` (`data_manager.py`): read `data_config.get("measurement_blocks",
  {})` in `__init__`; add a block-allocation loop in `_allocate_datasets()`
  (`data_manager.py:197-244`) right after the existing measurement-arrays
  loop, creating `(N, n_loop1, n_loop2, rows, cols)` NaN-filled resizable
  datasets; add a block-save branch in `save_datapoint()`
  (`data_manager.py:255-349`) alongside the existing
  `elif col_name in self._measurement_arrays` branch, validating shape and
  writing the value, with the same "pad the under-delivered axis with NaN,
  log a warning" fallback the array branch already has (`data_manager.py:305-325`)
  — but only ever padding the *rows* axis; a channel-axis mismatch is a hard
  `ValueError`, never padded. `close()`'s trim-to-actual-points loop
  (`data_manager.py:390-403`) needs no change — it already resizes axis 0
  generically regardless of trailing dimensionality.
- Update `DataManager.__init__`'s docstring `data_config` format example to
  show the new `measurement_blocks` key.

### 3. Wire it through the procedure (`cryosoft/core/procedure.py`)

- `SweepMeasureProcedure._build_data_schema()` (`procedure.py:1320-1345`):
  also read `vi.measurement_raw_blocks` and
  `vi.raw_block_row_counts(self._measurement_params)` to build
  `measurement_blocks: {name: (rows, len(labels))}` for the `DataSchema`.
- `initiate()` (`procedure.py:1347-1416`): when assembling the plain
  `data_config` dict handed to `DataManager`, add
  `"measurement_blocks": dict(self._data_schema.measurement_blocks)` and
  `"measurement_block_labels": dict(vi.measurement_raw_blocks)`. The labels
  need no new DataManager method — `_write_metadata()` already JSON-dumps
  the *entire* `data_config` dict as `/metadata.data_config`
  (`data_manager.py:194`), so the channel-name list rides along for free,
  the same way `tensor_component`/`readings_per_point` already do via
  `procedure_params`.
- `measure()` (`procedure.py:1432-1493`): extend the `keys = list(vi.
  measurement_data_keys) + list(vi.measurement_scalar_columns)` line
  (`procedure.py:1469`) to also include `list(vi.measurement_raw_blocks)`,
  so the existing generic per-loop-cell grid-building loop
  (`grids[key][i1][i2] = value`) captures block values from
  `take_reading()`'s dict the same uniform way it already handles scalars
  and arrays — no new branching needed there.

### 4. Tensormeter VI changes (`cryosoft/virtual_instruments/measurement/tensormeter_rtm2_measurement.py`)

- Add `_RAW_CHANNEL_NAMES`, a duplicated 44-name tuple matching the driver's
  `_DATA_COLUMNS` order — same layer-boundary reasoning already used for
  `_ANALYSIS_MODE_VALUES` (`tensormeter_rtm2_measurement.py:64-74`, the VI
  cannot import `cryosoft.drivers.*` per layer contract C3), same comment
  style.
- `measurement_raw_blocks: ClassVar[dict[str, list[str]]] = {"raw_channels_block":
  list(_RAW_CHANNEL_NAMES)}`.
- `raw_block_row_counts(self, params) -> dict[str, int]`: return
  `{"raw_channels_block": int(params["readings_per_point"])}`.
- `take_reading()` (`tensormeter_rtm2_measurement.py:579-675`): after the
  existing `rows = driver.read_new_data()[-n:]`, build `block = [[float(row[c])
  for c in _RAW_CHANNEL_NAMES] for row in rows]`, then NaN-pad to `n` rows on
  under-delivery — mirroring the existing `pad = n - len(rows)` pattern used
  for `res_a`/`res_b` a few lines below. Add `"raw_channels_block": block` to
  the returned dict. Every existing `res_a_ohm`/`res_b_ohm`/error/array
  computation and the `tensor_component` selection logic stay untouched.
- Update the module's YAML-style `output:` docstring header
  (`tensormeter_rtm2_measurement.py:35-45`) to mention the new field.

### 5. Standards documentation

- `cryosoft/virtual_instruments/measurement/README.md`: document the new
  "raw diagnostic block" convention (Interface contract section) alongside
  the existing mean/error/array convention.
- `GLOSSARY.md`: add an entry for "raw diagnostic block" /
  `measurement_raw_blocks` next to the existing mean/error/array term
  (`GLOSSARY.md:17-18`).

### 6. Tests

- `tests/test_conformance.py`: extend `test_measurement_vi_round_trip`
  (`test_conformance.py:1681-1725`) — fold `vi_cls.measurement_raw_blocks`
  keys into `expected_keys` (line 1695) and add a shape-check loop for
  blocks paralleling the existing array length-check (lines 1706-1710),
  using `raw_block_row_counts(defaults)` and `len(labels)` for the expected
  shape. Add a small new test asserting block names never collide with
  `measurement_data_keys`/`measurement_scalar_columns` names across every
  measurement VI, and that declared label lists are non-empty. This is
  additive coverage for the new convention, not a weakening of the existing
  one — the existing `test_measurement_vi_mean_error_array_convention`
  (`test_conformance.py:1625-1650`) is untouched, since it only ever walks
  `measurement_data_keys`.
- `tests/test_tensormeter_measurement_vi.py`: update `_EXPECTED_KEYS`
  (lines 27-31) to include `"raw_channels_block"`; add assertions on the
  block's `(readings_per_point, 44)` shape, its NaN-padding on
  under-delivery (mirroring the existing under-delivery test around lines
  296-321), and a cross-check that `block[i][_RAW_CHANNEL_NAMES.index(f"res_a_{component}_ohm")]
  == res_a_ohm_array[i]` for the active `tensor_component` — tying the two
  representations together as a regression guard.
- `tests/test_plan.py`: add `DataSchema.measurement_blocks` validation
  tests (valid/invalid shapes, `__post_init__` type/value errors,
  `validate()` catching shape mismatches) mirroring the existing
  `measurement_arrays` tests in the same file.
- `tests/test_l5_data_manager.py`: add a save/read-back round trip for a
  block column — verify HDF5 shape, NaN padding on a short row-count
  save, and that `close()`'s trim-to-actual-points still works with the
  extra dimension.

## Verification

1. `pytest -m "not hardware"` (or targeted: `pytest tests/test_plan.py
   tests/test_l5_data_manager.py tests/test_tensormeter_measurement_vi.py
   tests/test_conformance.py`) — new and existing tests green.
2. `make check` (ruff + lint-imports + full non-hardware suite) — confirms
   no layer-contract violation was introduced (this change stays within
   L1/L3/L5, no new cross-layer import).
3. Run the app against `sim_cryostat` (or the 12t-cryo config used
   previously), start a short Field Sweep with `tensormeter_measurement`
   selected, `readings_per_point` small (e.g. 2). After the run, open the
   resulting `.h5` and confirm: `raw_channels_block` dataset exists with
   shape `(N, 1, 1, readings_per_point, 44)`, `/metadata.data_config`
   contains `measurement_block_labels.raw_channels_block` as the 44 ordered
   names, and `res_a_ohm`/`res_b_ohm` still populate exactly as before
   (unchanged values) — reusing the same inspection approach already used
   earlier in this session (`h5py.File(...)` script over `/data` and
   `/metadata`).
4. Confirm the Procedure window's live plot still lists `res_a_ohm`/
   `res_b_ohm`/etc. in the Y-axis dropdown unchanged (the block is excluded
   from plotting automatically, the same way `*_array` columns already are,
   since it's never added to `measurement_data_keys`/`measurement_scalar_columns`).
