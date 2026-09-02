# Control UI groups: grouped rendering from method declarations

**Status:** intent brief, for planning. No code yet. This document states
what we want, why, and the shape of how, with evidence anchored in the
current tree. It is the input to a fuller refactor plan; it is not that
plan.
**Date:** 2026-09-02
**Scope:** the declaration and rendering of Virtual Instrument (VI)
`@monitored` / `@control` surfaces in the GUI, and the reuse of that same
declaration by the agent capability manifest. Nothing below the GUI changes
behaviour.
**Reconciles:** `deferred/complete-instrument-vis.md` (adopts its UI-group
primitive, drops the rest from this scope) and
`agentic-instrumentation-framework.md` Phase 1 (the manifest consumes the
groups).

---

## 1. Intent: what we want

A VI's methods should be able to declare, in the decorator that already
marks them, which **titled group** they belong to. The GUI renders one
titled box per declared group, in declared order, with the group's
monitored values and controls inside it. Methods that declare no group
render exactly as today. The same declaration is emitted by the capability
manifest so an agent sees the instrument as a small number of named
capability groups, each listing the actions and readings it contains,
rather than as one flat alphabetical list.

Concretely, for a Keithley 6221 + 2182A delta-mode VI, an operator opening
the front panel should see a box titled "Delta mode" holding the arming
control, the per-reading current setter, and the delta readbacks, instead
of those items interleaved alphabetically with everything else the VI
exposes. An agent reading the manifest should see the same box as one
schema object with a title and a description.

This is a **presentation and description** feature. It introduces no
runtime concept: no grouped value object, no atomic multi-parameter
dispatch, no cross-parameter validation, no mode exclusivity. Groups exist
in class metadata, in the GUI layout, and in the manifest. They do not
exist in the action queue, the Station, the Orchestrator, the HDF5 file,
or the run record.

## 2. Why: the evidence

Every claim below is anchored in the current tree. Re-verify anchors before
planning; they rot.

### 2.1 The GUI has no grouping and no ordering

- `InstrumentPanel._build_layout()` (`cryosoft/gui/instrument_panel.py:126`)
  emits every monitored value as one flat row, then every visible control
  as one flat row. There is no sub-box, no section heading, no ordering
  hook.
- Discovery is `get_control_methods()` / `get_monitored_methods()`
  (`cryosoft/core/decorators.py:170`, `:183`), which iterate `dir()`. The
  front panel therefore lists controls **alphabetically**, not in the order
  the physicist thinks in (arm, then set, then stop).
- The front panel (`cryosoft/gui/instrument_front_panel.py:74-80`) is the
  same `InstrumentPanel` with the `panel=False` filter disabled, so any
  grouping must live in `InstrumentPanel` to reach both surfaces.
- A many-argument control already gets special layout treatment inside its
  own row (`_build_control_row`, `instrument_panel.py:325`: a grid, two
  columns above ten widgets). That solves "one control with seven knobs".
  It does not solve "which controls belong together".

### 2.2 The metadata that drives the GUI is the metadata an agent needs

- `@control` attaches `_is_control`, `_display_name`, `_control_scope`,
  `_control_panel`, `_control_specs`, `_control_params`
  (`decorators.py:71`). `@monitored` attaches `_is_monitored` and
  `_display_name` only (`decorators.py:47`).
- `agentic-instrumentation-framework.md` §2.1 and §4 Phase 1 propose
  `core/capability_manifest.py` with `build_manifest(station)` reading
  exactly this metadata (plus `control_param_specs()`,
  `virtual_instruments/base.py:799`) to render the instrument surface as
  JSON Schema. A group tag in the same metadata is emitted by the same
  generator with no second declaration. That is the "declare once, render
  everywhere" property this repository is built on.
- Without groups, an agent gets a flat list of roughly a dozen controls per
  rich VI with no structure telling it which ones form a workflow. With
  groups, the manifest can present "Delta mode: arm → set current → stop"
  as one object, which is the progressive-disclosure shape agents handle
  well.

### 2.3 A worked example of the pain: delta mode

`cryosoft/virtual_instruments/measurement/measurement_delta_mode.py`:

- `initiate_measurement` (`:189-199`) takes seven arguments forwarded in one
  driver call. `set_delta_current` (`:238`) re-arms the engine from a shadow
  copy of the same seven. `take_reading` is deliberately neither monitored
  nor a control. Plus `standby`, `initiate`, connection controls, and
  several monitored readbacks.
- On the front panel these appear alphabetically, mixed with lifecycle
  controls. The operator has to know which three of them are "delta mode".

### 2.4 An adjacent defect worth fixing in the same series

Every measurement VI declares rich `ParamSpec`s in `measurement_parameters`
(`base.py:1248`; delta mode at `measurement_delta_mode.py:100-148`,
including a `choices` map for the voltmeter range), but **none** of the six
`initiate_measurement` controls declares `params=` on `@control`
(verified by grep across `virtual_instruments/measurement/`). The front
panel therefore renders bare `QLineEdit`s for the arming control and drops
the choices, while the procedure form renders the same parameters correctly
from `measurement_parameters`. This is not grouping, but it is the same
"one declaration, several renderers" principle, and the fix is small:
`MeasurementInstrumentBase` can install `measurement_parameters` as the
arming control's specs at class creation. Anyone planning the group work
should plan this alongside it, because both touch the same three files.

### 2.5 What already exists and must not be duplicated

- **`ParamGroup`** (`cryosoft/core/plan.py:450`) is a *procedure-form*
  box: a keyed set of `ParamSpec`s rendered by `gui/param_form.py:288`
  `build_group_box()` and diffed by key on structural re-render. It groups
  the parameters of one procedure run. It is not a VI concept and should
  not be overloaded to become one; `deferred/complete-instrument-vis.md`
  made the same call and named the VI primitive `UIGroup`.
- **`monitor.yaml` `panels:`** (`gui/monitor_window.py:171-199`,
  conformance `tests/test_conformance.py:1001`) is a per-setup allowlist
  of which controls appear on the compact card. It decides *visibility*,
  not *structure*.
- **`synchronized-sweep-variables.md`** extends `ParamGroup` with `axes`
  for the procedure form. It is orthogonal to this work and must stay
  untouched by it.

## 3. Non-goals (explicit, so the planner does not drift)

1. **No grouping below the GUI.** No group-valued object crosses
   `submit_vi_action()`, the action queue, `Station.execute_vi_action()`,
   or any driver. `kwargs` stay flat scalars keyed by parameter name.
2. **No atomicity claims.** One control is already one method call. Nothing
   here makes several controls, or several VIs, atomic.
3. **No cross-parameter validation hook.** `control_limits` stays per
   scalar; semantic rules stay as explicit raises at the top of the method.
4. **No mode exclusivity, no `@query` verb.** Both are in the deferred plan
   and both change runtime behaviour. They are out of scope; if wanted
   later they layer on top of groups without changing them.
5. **No persistence change.** HDF5 metadata, `RunRecord.params`, form
   autosave keys are untouched.
6. **No recursion.** One level: a VI has groups, a group has methods.
7. **No per-setup group override in YAML.** Group membership is a property
   of the instrument's capability surface, so it lives in code. `panels:`
   keeps deciding visibility only.

## 4. How: the shape of the design

This is the shape, with the constraints that fix it. The planner owns the
detail, the sequencing, and the tests.

### 4.1 Declaration

- `@monitored(group="delta")` and `@control(group="delta", ...)`: an
  optional keyword storing a **plain string** `_ui_group` on the function.
  `core/decorators.py` may not import `ParamSpec` or any spec type (layer
  contract C1), so the tag is opaque here, exactly like `_control_specs`.
  Bare `@monitored` keeps working (it is a plain function decorator today;
  it needs the same bare-or-parametrized form `@control` already has).
- `UIGroup(key, title, description="")` as a frozen dataclass in
  `core/plan.py`, following the house style there (eager `__post_init__`
  validation, non-empty key and title). `plan.py` is importable by the GUI
  (C8), the Orchestrator (C5), and the Station (C4), so it needs no new
  contract row.
- `ui_groups: ClassVar[tuple[UIGroup, ...]] = ()` on
  `BaseVirtualInstrument`. Declared order is render order and manifest
  order. Subclasses extend by tuple concatenation, mirroring the
  `control_limits` merge convention.
- Read helper `get_ui_group(method) -> str` beside the existing
  `get_control_panel` / `get_control_specs` helpers.

### 4.2 Validation at class creation

`BaseVirtualInstrument.__init_subclass__` (`base.py:267`) already
type-checks `_control_specs` and wraps controls. Extend it to check:
group keys unique; every `_ui_group` tag on a monitored or control method
names a declared `UIGroup`. Fail loudly with a message naming the class,
method, and tag. Wrappers must preserve `_ui_group` the same way they
preserve the other markers today.

Conformance (`tests/test_conformance.py`): every discovered VI passes the
above; every `UIGroup` has a non-empty title; a moved or renamed method
cannot leave a dangling tag. Existing VIs declare no groups and pass
vacuously.

### 4.3 Rendering

- `InstrumentPanel._build_layout()`: for each declared group in order,
  build one titled box containing that group's monitored rows then its
  visible control rows, reusing the existing row builders. Then the
  ungrouped monitored rows and ungrouped control rows exactly as today, so
  a VI with no groups is byte-identical. Ordering *within* a group follows
  the group's own declared method order if `UIGroup` carries one, else
  declaration order in the class body. Alphabetical is never the answer.
- Reuse `param_form.build_group_box()`-style `QGroupBox` construction and
  the theme's group-box tokens; follow the `gui-edit` skill (theme tokens
  only, dynamic properties, offscreen screenshot verification).
- Open decision, see §6: whether the compact monitor card also shows group
  boxes or stays flat with only the front panel grouped.

### 4.4 Manifest

When Phase 1 of the agentic framework lands `build_manifest(station)`, each
VI entry carries `groups: [{key, title, description, monitored: [...],
controls: [...]}]` in declared order, followed by ungrouped items. The
group tag comes from the same `_ui_group` attribute the GUI reads. No
second declaration, no hand-maintained mapping. If this work lands before
Phase 1, it ships a small `ui_groups`-aware helper that Phase 1 then calls.

### 4.5 The adjacent fix (§2.4)

`MeasurementInstrumentBase.__init_subclass__` installs
`measurement_parameters` as `_control_specs` on `initiate_measurement` when
that method declares no `params=` of its own, so the front panel renders
the same widgets the procedure form does. The existing conformance test
`test_control_declarations_are_consistent` (`test_conformance.py:1305`)
then covers it automatically.

## 5. Acceptance criteria

1. A VI with no `ui_groups` and no tags renders and tests identically to
   before. Verified by the existing GUI tests and an offscreen screenshot
   diff.
2. The delta-mode VI (and at least one temperature-controller VI, which
   has `set_pid` and heater controls that belong together) declares groups
   and the front panel shows them as titled boxes in declared order.
3. A dangling or duplicate group tag fails at import, and the conformance
   suite fails for it, with a message naming the method.
4. The manifest helper emits the groups from the same metadata, and a test
   asserts one VI's manifest groups equal its `ui_groups` declaration.
5. The arming control of every measurement VI renders `choices` and units
   on the front panel.
6. `make check` green after every commit; GLOSSARY rows for **UI group**
   and the updated `@monitored` / `@control` row; `gui/README.md` and
   `virtual_instruments/README.md` updated in the same commits as the code.

## 6. Decisions the planner needs from the owner

1. **Card versus front panel.** Group boxes on the compact monitor card
   make the grid taller. Recommendation: card stays flat and honours
   `panels:` as today; only the front panel renders boxes.
2. **Ordering within a group.** Either `UIGroup` carries an explicit
   `members` tuple (more typing, exact order, doubles as validation), or
   order follows class-body declaration order (less typing, needs the
   class-creation hook to record it). Recommendation: explicit `members`,
   because it also documents the workflow order for the agent.
3. **Name.** `UIGroup` (the deferred plan's name) versus something like
   `ControlGroup`. Recommendation: keep `UIGroup`; it already appears in a
   written plan and it groups monitored values too.

## 7. Relationship to the other plans

- `deferred/complete-instrument-vis.md`: this brief **takes** its Wave 1
  group primitive (`UIGroup`, `group=` tag, `ui_groups`, class-creation
  validation) and Wave 3's group-box rendering. It **leaves** `@query`,
  `QuerySchema`, mode exclusivity, the bench VI role, and the waveform
  driver work in that document, untouched and still deferred. When this
  brief is implemented, that document should be edited to say its group
  primitive has landed and to point here.
- `agentic-instrumentation-framework.md`: Phase 1's manifest is the second
  consumer of the group metadata. This brief does not block Phase 1 and
  Phase 1 does not block it; whichever lands second adds the plumbing.
- `synchronized-sweep-variables.md`: untouched. `ParamGroup` remains the
  procedure-form concept.
