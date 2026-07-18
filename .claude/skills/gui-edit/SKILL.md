---
name: gui-edit
description: How to modify the CryoSoft GUI layer (cryosoft/gui/) correctly - theme tokens, Qt dynamic-property styling, layout rules, notification policy, and the mandatory offscreen screenshot verification. Use for any change to GUI widgets, styling, colors, layout, or GUI tests.
---

# gui-edit — editing the CryoSoft GUI layer

The GUI was rebuilt in July 2026 (light theme, layered on validated design
tokens). These rules keep edits consistent with that system. Layer rules are
machine-enforced: contract C8 in `pyproject.toml` (import-linter) forbids the
GUI from importing drivers or concrete VIs (`virtual_instruments.base` is the
one allowed exception). CI runs `make lint + make contracts + make test`.

## Files

| File | Role |
|------|------|
| `cryosoft/gui/theme.py` | ALL colors and QSS live here (tokens + `build_stylesheet()`) |
| `cryosoft/gui/monitor_window.py` | Main window (composition shell): quadrant splitters, banner, status bar, menus, signal wiring |
| `cryosoft/gui/trends_quadrant.py` | Trends quadrant: TrendPlotPanel grid + MonitorHistory + persistence |
| `cryosoft/gui/session_info_panel.py` | Session Information quadrant (experiment Start/Close control + name/ID/comments/data dir) |
| `cryosoft/gui/experiment_dialogs.py` | Start/Close Experiment dialogs, add-user dialog |
| `cryosoft/gui/other_devices.py` | Other Devices rows (measurement check rows, display-only switch rows) |
| `cryosoft/gui/log_panel.py` | Log widget + `QtLogHandler` (attach/detach lifecycle) |
| `cryosoft/gui/config_menu.py` | Config menu controller (select/switch/restart, editor launcher) |
| `cryosoft/gui/procedure_window.py` | Procedure window (composition shell): quadrants, run/queue/abort flows, two `LivePlotPanel`s |
| `cryosoft/gui/procedure_params_panel.py` | Procedure selector + parameter form + param cache (the params quadrant) |
| `cryosoft/gui/queue_panel.py` | Run-queue list, per-item status, Orchestrator queue sync |
| `cryosoft/gui/procedure_discovery.py` | Qt-free BaseProcedure auto-discovery |
| `cryosoft/gui/window_geometry.py` | Shared geometry restore/save helpers for both windows |
| `cryosoft/gui/instrument_panel.py` | Auto-generated per-VI panel (from decorator metadata) |
| `cryosoft/gui/live_plot_panel.py` | Reusable X/Y live plot widget |
| `cryosoft/gui/notification_banner.py` | Non-modal warning/error strip |
| `cryosoft/gui/app_settings.py` | QSettings factory — the test seam |
| `tests/test_gui.py` | pytest-qt suite; run with `-p no:randomly` when run alone |

Destruction-order rule: Orchestrator signals that feed child panels (e.g.
`states_updated` → Trends / Other Devices) must connect to a WINDOW slot that
forwards to the panels, never to the panel directly — Qt severs a receiver's
connections at the start of its own destruction, so the window-as-receiver
topology is what stops a live tick from reaching a partially destroyed child
tree (RuntimeError/segfault on a deleted plot curve under pytest-qt).

## Styling rules

1. **Never call `setStyleSheet` on a widget.** All styling goes through tokens
   and rules in `theme.py`. State-dependent styling uses a dynamic property +
   QSS attribute selector (`QGroupBox[status="stale"]`), set only on actual
   state transitions.
2. **Repolish the children too.** After `setProperty(...)`, run
   `style().unpolish(w); style().polish(w)` on the widget AND on every child
   that a descendant selector targets (e.g. the QLabel inside the banner).
   Parent-only repolish leaves children with stale colors — this bug shipped
   once already; regression tests now assert *effective* palette colors
   (`label.palette().windowText().color().name()`), not just the property.
3. **No layout jumps from state styling.** If a state adds a border, the base
   rule must reserve the same border width in transparent
   (`border: 2px solid transparent`).
4. **No invented colors.** The palette was validated (WCAG contrast + Machado
   CVD simulation). New color = compute WCAG contrast first: >= 4.5:1 for text,
   >= 3:1 for non-text marks, against the actual surface it renders on. Plot
   curves come from `PLOT_SERIES` in order.
5. Icons: `qtawesome.icon("fa5s.<name>", color=<button text color>)`; every
   action button gets a tooltip. Do not cap a button's width below its size
   hint once it carries an icon.

## Layout rules

- Every `QSplitter`: `setChildrenCollapsible(False)` + minimum sizes on panes.
- Never wrap a whole window in a `QScrollArea`; scroll only the region that
  can genuinely overflow (the VI grid).
- Widgets that display data get real minimum sizes so they cannot be crushed.
- `findChild` objectNames are API — tests and muscle memory rely on them.
  Preserve them through refactors (thread them through constructors, as
  `LivePlotPanel` does).

## Behavior rules

- Orchestrator-signal events (`error_occurred`, `action_blocked`) go to the
  `NotificationBanner`, never to modal dialogs. Modals only confirm a direct
  user click (e.g. Abort).
- GUI talks ONLY to the Orchestrator's public API. No `_private` access, no
  direct VI method calls. If the capability is missing, add it to the
  Orchestrator first (separate, core-layer task — do not shortcut).
- Geometry/settings persistence goes through `app_settings.get_settings()`.
  Never construct `QSettings(...)` directly in a window: tests monkeypatch the
  factory (autouse `isolated_settings` fixture) so pytest runs cannot clobber
  the user's saved geometry in the registry.

## Verification (all three, every time)

1. `& "<repo>\.venv\Scripts\python.exe" -m pytest tests/test_gui.py -q -p no:randomly`
2. Full suite: `... -m pytest tests/ -q`
3. **Offscreen screenshot smoke — mandatory for any visible change:**
   `& "<repo>\.venv\Scripts\python.exe" .claude\skills\gui-edit\scripts\gui_smoke.py`
   writes PNGs of both windows (idle + error/stale/emergency states) to
   `tmp/gui-edit/<timestamp>/` and prints effective banner/status-bar colors.
   Open the PNGs and look at them. Tests assert properties; only pixels catch
   wrong-looking output. (Blocky glyphs in the PNGs are an offscreen-platform
   font fallback, not a real defect.)
