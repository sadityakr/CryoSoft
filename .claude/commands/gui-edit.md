You are making a visual edit to the CryoSoft PyQt6 GUI.

## Step 1 — Orient yourself

Before touching any code, read these two files in order:

1. `cryosoft/gui/README.md` — architecture rules, file map, signal→widget table.
2. The specific GUI file you will edit (see Step 2).

Do not skip this. The README tells you which file owns which widget and which signals drive which updates.

## Step 2 — Identify the target

The user's request is: **$ARGUMENTS**

Based on the request, identify:
- Which file contains the widget or layout to change (`instrument_panel.py`, `monitor_window.py`, or `procedure_window.py`).
- Which method builds or updates that widget.
- Whether the change is purely visual (stylesheet, label text, layout spacing) or behavioural (connects a new signal, adds a new widget that must emit or receive).

If the target is ambiguous, ask one clarifying question before proceeding.

## Step 3 — Hard constraints (read before editing)

These rules come from `cryosoft/gui/README.md` and **must not be violated**:

- **Never import from `cryosoft.drivers.*`** in any GUI file.
- **Never import from `cryosoft.virtual_instruments.*`** except `BaseVirtualInstrument` for type annotations.
- **Never call a VI method directly** from the GUI. All instrument commands go through `orchestrator.submit_vi_action()` or `orchestrator.submit_global_action()`.
- **Never block the Qt event loop** — no `time.sleep()`, no synchronous I/O inside a slot.
- **Never add a new Orchestrator signal** unless the data genuinely does not exist anywhere in the current signal set. Check the signal→widget table in the README first.

## Step 4 — Make the edit

Edit only the file(s) identified in Step 2. Do not refactor surrounding code, rename variables, or add features beyond what was requested.

For purely visual changes (colour, font, spacing, label text):
- Edit the relevant style string or layout constant directly.
- Do not change method signatures or widget object names (tests use object names to find widgets).

For widget additions (new label, new button, new plot element):
- Add the widget in the same `_build_*` method that builds the surrounding section.
- Assign a unique `objectName` following the existing pattern: `f"{vi_name}_{method_name}_suffix"` or `"section_element_btn"`.
- If the widget must update at runtime, connect it to an existing Orchestrator signal in `_connect_signals()`.

For layout changes (reorder, resize, add column):
- Keep `_COLUMNS = 2` in `monitor_window.py` unless the user explicitly asked to change the column count.
- Preserve `setSpacing` and `setContentsMargins` values unless the user explicitly asked to change them.

## Step 5 — Update the front matter

The edited file has a YAML front matter block at the top. If the change affects the file's inputs, outputs, or visible behaviour, update `last_updated` to today's date (2026-04-06). Cosmetic-only changes (colour, spacing) do not require a front matter update.

## Step 6 — Run the GUI tests

After editing, run:

```
cd "c:/Users/sadit/OneDrive - JGU/Projects/Tools/Cryosoft" && source .venv/Scripts/activate && python -m pytest tests/test_gui.py -v
```

All 22 tests must pass. If any fail:
- Read the failure message carefully.
- Check whether you changed an `objectName` (tests use them for widget lookup).
- Fix the issue and re-run. Do not mark the task done until all tests are green.

## Step 7 — Report

Tell the user:
1. Which file and method you edited.
2. What the visual change is in plain language.
3. The test result (`22/22 passed` or the failure you found and fixed).

Do not summarise unchanged code or list every line you modified.
