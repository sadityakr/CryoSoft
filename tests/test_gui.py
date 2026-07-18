# ---
# description: |
#   Smoke tests for the CryoSoft GUI layer (Layer 6).
#   Verifies that MonitorWindow and ProcedureWindow open without errors,
#   that InstrumentPanel widgets are auto-generated for all registered VIs,
#   and that Orchestrator signals (state_changed, procedure_progress,
#   measurement_ready) update the GUI correctly.
# last_updated: 2026-07-13
# ---

"""GUI smoke tests — Layer 6.

These tests use pytest-qt (qtbot fixture). They run against the sim_cryostat
config with no hardware. All 121 prior tests must pass before this file is run.
"""

import logging
from pathlib import Path

import pytest
from PyQt6.QtCore import Qt, QSettings
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QTextEdit,
    QWidget,
)

from cryosoft.core.config_catalog import ConfigCatalog
from cryosoft.core.orchestrator import Orchestrator, OrchestratorState
from cryosoft.core.station import build_station
from cryosoft.gui import app_settings as _app_settings
from cryosoft.gui import form_autosave as session_store
from cryosoft.gui import window_geometry
from cryosoft.gui.instrument_panel import InstrumentPanel
from cryosoft.gui.monitor_window import MonitorWindow
from cryosoft.gui.diagnostics_window import DiagnosticsWindow
from cryosoft.gui.notification_banner import NotificationBanner
from cryosoft.gui.procedure_window import ProcedureWindow
from cryosoft.gui.theme import (
    BANNER_ERROR_TEXT,
    BANNER_WARNING_TEXT,
    TEXT_ON_ACCENT,
    TEXT_PRIMARY,
    build_stylesheet,
)
from cryosoft.gui.trend_plot_panel import TrendPlotPanel


CONFIG_PATH = "cryosoft/configs/sim_cryostat"


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    """Redirect the app QSettings factory to a throwaway INI file.

    Dependency seam: both windows call ``app_settings.get_settings()`` for
    geometry persistence. Monkeypatching that factory to an INI file under
    ``tmp_path`` means a pytest run never reads or overwrites the user's real
    saved geometry in the Windows registry. Autouse so every GUI test is
    isolated without opting in.

    Yields:
        The Path of the throwaway INI file, so a test can inspect what was
        written to it.
    """
    from cryosoft.gui import app_settings

    ini_path = tmp_path / "cryosoft_test_settings.ini"

    def _fake_get_settings():
        return QSettings(str(ini_path), QSettings.Format.IniFormat)

    monkeypatch.setattr(app_settings, "get_settings", _fake_get_settings)

    # Same seam for the JSON session file: redirect it into tmp_path so a pytest
    # run never reads or overwrites the user's real last_session.json in AppData.
    session_path = tmp_path / "last_session.json"

    def _fake_session_file_path(user_id=None):
        return (tmp_path / "sessions" / f"{user_id}.json") if user_id else session_path

    monkeypatch.setattr(app_settings, "session_file_path", _fake_session_file_path)
    return ini_path


@pytest.fixture
def station():
    """Real simulated station from sim_cryostat config."""
    return build_station(CONFIG_PATH)


@pytest.fixture
def orchestrator(station, qtbot):
    """Orchestrator with a short tick for fast tests.

    Monitoring stays OFF (the production launch state): GUI tests drive
    updates by emitting Orchestrator signals directly, so they need no real
    polling ticks. The teardown stops the tick timer entirely, so a tick can
    never fire into the half-destroyed widget tree while qtbot tears the
    windows down (the historical source of rare RuntimeError/segfault flakes).
    """
    orch = Orchestrator(station, tick_interval_ms=50)
    yield orch
    orch.shutdown()


@pytest.fixture
def monitor_win(station, orchestrator, qtbot):
    """MonitorWindow shown via qtbot."""
    win = MonitorWindow(station, orchestrator)
    qtbot.addWidget(win)
    win.show()
    return win


@pytest.fixture
def diagnostics_win(orchestrator, qtbot):
    """DiagnosticsWindow shown via qtbot."""
    win = DiagnosticsWindow(orchestrator)
    qtbot.addWidget(win)
    win.show()
    return win


@pytest.fixture
def procedure_win(station, orchestrator, qtbot):
    """ProcedureWindow shown via qtbot with stub sample-info callables."""
    def _sample_info():
        return {"sample_name": "test", "sample_id": "T001", "comments": ""}

    def _data_dir():
        return "C:/CryoData"

    win = ProcedureWindow(
        station, orchestrator,
        get_sample_info=_sample_info,
        get_data_dir=_data_dir,
    )
    qtbot.addWidget(win)
    win.show()
    return win


# ── MonitorWindow tests ───────────────────────────────────────────────────────

def test_monitor_window_opens(monitor_win):
    """MonitorWindow constructs and is visible."""
    assert monitor_win.isVisible()


def test_monitor_window_has_panels_for_system_vis(monitor_win, station):
    """One InstrumentPanel exists per system/level VI (measurement VIs use status cards)."""
    panels = monitor_win.findChildren(InstrumentPanel)
    system_vis = [n for n in station.get_vi_names() if station.get_vi_type(n) in {"system", "level"}]
    assert len(panels) == len(system_vis), (
        f"Expected {len(system_vis)} panels, found {len(panels)}"
    )


def test_monitor_window_panel_titles_match_system_vi_names(monitor_win, station):
    """Each InstrumentPanel title matches a system/level VI name."""
    panels = monitor_win.findChildren(InstrumentPanel)
    panel_titles = {p._vi_name for p in panels}
    system_vis = {n for n in station.get_vi_names() if station.get_vi_type(n) in {"system", "level"}}
    assert panel_titles == system_vis


def test_monitor_window_has_global_buttons(monitor_win):
    """Initiate All and Standby All buttons exist."""
    initiate_btn = monitor_win.findChild(QPushButton, "initiate_all_btn")
    standby_btn = monitor_win.findChild(QPushButton, "standby_all_btn")
    assert initiate_btn is not None, "initiate_all_btn not found"
    assert standby_btn is not None, "standby_all_btn not found"


def test_status_bar_updates_on_state_change(monitor_win, orchestrator, qtbot):
    """MonitorWindow status bar label reflects Orchestrator state."""
    orchestrator.state_changed.emit("RAMPING")
    assert "RAMPING" in monitor_win._state_label.text()


# ── InstrumentPanel tests ─────────────────────────────────────────────────────

def test_instrument_panel_creates_value_labels(station, orchestrator, qtbot):
    """InstrumentPanel creates one QLabel per @monitored method."""
    from cryosoft.core.decorators import get_monitored_methods

    vi_name = "magnet_x"
    vi = station._virtual_instruments[vi_name]
    panel = InstrumentPanel(vi_name, vi, orchestrator)
    qtbot.addWidget(panel)

    monitored = get_monitored_methods(vi)
    for method_name in monitored:
        widget = panel.findChild(QLabel, f"{vi_name}_{method_name}_value")
        assert widget is not None, f"Missing value label for {method_name}"


def test_instrument_panel_creates_control_buttons(station, orchestrator, qtbot):
    """InstrumentPanel creates one QPushButton per @control method."""
    from cryosoft.core.decorators import get_control_methods

    vi_name = "magnet_x"
    vi = station._virtual_instruments[vi_name]
    panel = InstrumentPanel(vi_name, vi, orchestrator)
    qtbot.addWidget(panel)

    controls = get_control_methods(vi)
    for method_name in controls:
        btn = panel.findChild(QPushButton, f"{vi_name}_{method_name}_btn")
        assert btn is not None, f"Missing button for {method_name}"


def test_instrument_panel_lifecycle_buttons_exist(station, orchestrator, qtbot):
    """InstrumentPanel has a single lifecycle toggle button (Initiate/Standby)."""
    vi_name = "temperature_vti"
    vi = station._virtual_instruments[vi_name]
    panel = InstrumentPanel(vi_name, vi, orchestrator)
    qtbot.addWidget(panel)

    assert panel.findChild(QPushButton, f"{vi_name}_lifecycle_btn") is not None
    assert panel.findChild(QPushButton, f"{vi_name}_initiate_btn") is None
    assert panel.findChild(QPushButton, f"{vi_name}_standby_btn") is None


def test_instrument_panel_updates_values_on_signal(station, orchestrator, qtbot):
    """states_updated signal → value labels reflect new state."""
    vi_name = "magnet_x"
    vi = station._virtual_instruments[vi_name]
    panel = InstrumentPanel(vi_name, vi, orchestrator)
    qtbot.addWidget(panel)

    # Emit a fake state with known field value
    fake_state = {vi_name: {"get_field": 1.5, "magnet_current": 15.0, "magnet_status": "HOLD"}}
    orchestrator.states_updated.emit(fake_state)

    field_label = panel.findChild(QLabel, f"{vi_name}_get_field_value")
    if field_label is not None:
        assert "1.5" in field_label.text()


def test_instrument_panel_stale_border(station, orchestrator, qtbot):
    """Stale state sets the 'stale' status property (amber border via QSS)."""
    vi_name = "magnet_x"
    vi = station._virtual_instruments[vi_name]
    panel = InstrumentPanel(vi_name, vi, orchestrator)
    qtbot.addWidget(panel)

    orchestrator.states_updated.emit({vi_name: {"_stale": True}})
    assert panel.property("status") == "stale"
    assert "[stale]" in panel._name_label.text()


def test_instrument_panel_disconnected_border(station, orchestrator, qtbot):
    """Disconnected state sets the 'disconnected' status property (red border via QSS)."""
    vi_name = "magnet_x"
    vi = station._virtual_instruments[vi_name]
    panel = InstrumentPanel(vi_name, vi, orchestrator)
    qtbot.addWidget(panel)

    orchestrator.states_updated.emit({vi_name: {"_stale": True, "_disconnected": True}})
    assert panel.property("status") == "disconnected"
    assert "[DISCONNECTED]" in panel._name_label.text()


def test_instrument_panel_status_resets_to_ok(station, orchestrator, qtbot):
    """A stale panel returns to 'ok' status (plain title) when state is healthy again."""
    vi_name = "magnet_x"
    vi = station._virtual_instruments[vi_name]
    panel = InstrumentPanel(vi_name, vi, orchestrator)
    qtbot.addWidget(panel)

    orchestrator.states_updated.emit({vi_name: {"_stale": True}})
    assert panel.property("status") == "stale"

    orchestrator.states_updated.emit({vi_name: {}})
    assert panel.property("status") == "ok"
    assert panel._name_label.text() == f"<b>{vi_name}</b>"


def test_instrument_panel_status_not_restyled_when_unchanged(
    station, orchestrator, qtbot, monkeypatch
):
    """The status property is only re-set when it changes, not on every tick."""
    vi_name = "magnet_x"
    vi = station._virtual_instruments[vi_name]
    panel = InstrumentPanel(vi_name, vi, orchestrator)
    qtbot.addWidget(panel)

    status_sets: list[object] = []
    original_set = panel.setProperty

    def spy(name, value):  # type: ignore[no-untyped-def]
        if name == "status":
            status_sets.append(value)
        return original_set(name, value)

    monkeypatch.setattr(panel, "setProperty", spy)

    # First tick changes ok -> stale (one set); the next two are unchanged.
    orchestrator.states_updated.emit({vi_name: {"_stale": True}})
    orchestrator.states_updated.emit({vi_name: {"_stale": True}})
    orchestrator.states_updated.emit({vi_name: {"_stale": True}})

    assert status_sets == ["stale"]
    assert panel.property("status") == "stale"


# ── ProcedureWindow tests ─────────────────────────────────────────────────────

def test_procedure_window_opens(procedure_win):
    """ProcedureWindow constructs and is visible."""
    assert procedure_win.isVisible()


def test_procedure_selector_populated(procedure_win):
    """Procedure selector has at least one entry (FieldSweep / TemperatureSweep)."""
    assert procedure_win._params_panel._proc_selector.count() >= 1


def test_procedure_param_inputs_exist(procedure_win):
    """Parameter form inputs are created for the selected procedure.

    FieldSweep declares sweep_axis, so its hidden axis parameters (field_mode,
    field_start, ...) are handled by the SweepAxisWidget, not a flat QLineEdit.
    Its measurement parameters are station-dependent (from the selected
    measurement VI), rendered under the "Measurement method" selector.
    """
    from cryosoft.procedures.field_sweep import FieldSweep

    _select_procedure(procedure_win, FieldSweep.name)

    assert procedure_win._params_panel._axis_widget is not None

    # System params (temperature, init_wait, step_wait) render as flat inputs.
    for param_name in FieldSweep.system_parameters:
        field = procedure_win.findChild(QWidget, f"param_{param_name}_input")
        assert field is not None, f"Missing input for parameter '{param_name}'"

    # The measurement-method selector renders (a structural combobox).
    assert procedure_win.findChild(QComboBox, "param_measurement_vi_input") is not None


def test_procedure_param_label_and_tooltip(procedure_win):
    """Param label is the canonical `name (unit):` and carries the description tooltip.

    The label is the same key stored under /metadata/procedure_params in the
    HDF5 output (see BaseProcedure), not prose. The prose description lives in
    a tooltip on both the input field and its form label.

    Uses FieldSweep's ``temperature`` system_parameter rather than one of
    its sweep_axis-generated fields (e.g. field_start): those are rendered by
    SweepAxisWidget, not a flat QLineEdit + QFormLayout row, so they are not a
    valid target for this label/tooltip check.
    """
    from cryosoft.procedures.field_sweep import FieldSweep

    _select_procedure(procedure_win, FieldSweep.name)

    spec = FieldSweep.system_parameters["temperature"]
    field = procedure_win.findChild(QLineEdit, "param_temperature_input")
    assert field is not None, "Missing input for parameter 'temperature'"

    assert field.text() == str(spec.default)

    form = field.parent().layout()
    assert isinstance(form, QFormLayout)
    row_label = form.labelForField(field)
    assert isinstance(row_label, QLabel)
    assert row_label.text() == "temperature (K):"

    for tooltip in (field.toolTip(), row_label.toolTip()):
        assert tooltip, "Tooltip must be non-empty"
        assert spec.description in tooltip


def _select_procedure(procedure_win, name):
    """Select the procedure whose exact display name is *name*."""
    for i in range(procedure_win._params_panel._proc_selector.count()):
        if procedure_win._params_panel._proc_selector.itemText(i) == name:
            procedure_win._params_panel._proc_selector.setCurrentIndex(i)
            return
    pytest.fail(f"{name!r} not found in procedure selector")


def test_procedure_enum_and_bool_widgets_render(procedure_win):
    """A 'choices' param renders a combobox of labels; a bool param a checkbox.

    Covers the delta-mode measurement parameters of the default measurement VI
    (keithley_delta_mode, first in the sim config): voltmeter_range_V
    (enumerated 2182A range) and the compliance_abort / cold_switch booleans.
    """
    from cryosoft.procedures.field_sweep import FieldSweep

    _select_procedure(procedure_win, FieldSweep.name)

    combo = procedure_win.findChild(QComboBox, "param_voltmeter_range_V_input")
    assert combo is not None, "voltmeter_range_V should render as a combobox"
    labels = [combo.itemText(i) for i in range(combo.count())]
    assert labels == ["10 mV", "100 mV", "1 V", "10 V", "100 V"]
    # Default 0.01 -> "10 mV" preselected.
    assert combo.currentText() == "10 mV"

    abort_box = procedure_win.findChild(QCheckBox, "param_compliance_abort_input")
    cold_box = procedure_win.findChild(QCheckBox, "param_cold_switch_input")
    assert abort_box is not None and cold_box is not None
    assert abort_box.isChecked() is True   # default True
    assert cold_box.isChecked() is False   # default False


def test_procedure_enum_and_bool_values_collected(procedure_win):
    """_collect_params maps a combobox label to its value and reads checkboxes."""
    from cryosoft.procedures.field_sweep import FieldSweep

    _select_procedure(procedure_win, FieldSweep.name)

    procedure_win.findChild(QComboBox, "param_voltmeter_range_V_input").setCurrentText("1 V")
    procedure_win.findChild(QCheckBox, "param_compliance_abort_input").setChecked(False)
    procedure_win.findChild(QCheckBox, "param_cold_switch_input").setChecked(True)

    collected = procedure_win._collect_params()
    assert collected is not None
    param_values = collected[0]
    # Combobox returns the *mapped* instrument value, not the label.
    assert param_values["voltmeter_range_V"] == pytest.approx(1.0)
    assert param_values["compliance_abort"] is False
    assert param_values["cold_switch"] is True


# ── Generic sweep procedure: structural measurement-VI re-render ──────────────

def _measurement_combo(win):
    """Return the measurement-method QComboBox on the current form."""
    combo = win.findChild(QComboBox, "param_measurement_vi_input")
    assert combo is not None, "measurement-method selector should be rendered"
    return combo


def _set_slot_parameter(win, slot_param_name, qualified):
    """Set a Reading-loop slot drop-down to the entry mapping to *qualified*."""
    combo = win.findChild(QComboBox, f"param_{slot_param_name}_input")
    assert combo is not None, f"{slot_param_name} selector should be rendered"
    groups = {g.key: g for g in win._params_panel._current_groups}
    spec = groups["reading_loop"].params[slot_param_name]
    label = next(k for k, v in spec.choices.items() if v == qualified)
    combo.setCurrentText(label)


def _select_measurement(win, vi_name):
    """Set the measurement combobox to the label whose mapped value is *vi_name*."""
    combo = _measurement_combo(win)
    for group in win._params_panel._current_groups:
        spec = group.params.get("measurement_vi")
        if spec is None:
            continue
        for label, value in spec.choices.items():
            if value == vi_name:
                combo.setCurrentText(str(label))
                return
    pytest.fail(f"measurement VI {vi_name!r} not in the selector")


def _settle_at_width(win, width=1280, height=800):
    """Resize the window to *width* x *height* and let the layout settle."""
    win.resize(width, height)
    win.show()
    QApplication.processEvents()


def _fully_inside_param_viewport(win, widget) -> bool:
    """Return True if *widget* is visible AND fully inside the param scroll viewport.

    Maps the widget's rectangle into the parameter scroll area's viewport
    coordinates; a widget scrolled off the right edge (the pre-fix overflow bug)
    maps to an x beyond the viewport width and fails this check. This is the
    geometry assertion that mere ``findChild`` existence checks did not catch.
    """
    if not widget.isVisible():
        return False
    viewport = win._params_panel._param_scroll.viewport()
    top_left = widget.mapTo(viewport, widget.rect().topLeft())
    bottom_right = widget.mapTo(viewport, widget.rect().bottomRight())
    return (
        top_left.x() >= 0
        and bottom_right.x() <= viewport.width()
        and bottom_right.x() > top_left.x()
    )


def test_generic_field_sweep_renders_measurement_select_and_default_group(procedure_win):
    """The form shows the measurement-method combo + the default VI's param group.

    The default measurement VI is the first registered one (keithley_delta_mode
    in the sim config), so its delta-mode parameters render inside the single
    composite "Measurement" column.
    """
    from cryosoft.procedures.field_sweep import FieldSweep

    _select_procedure(procedure_win, FieldSweep.name)

    combo = _measurement_combo(procedure_win)
    assert combo.count() == 3  # keithley_delta_mode + dc_measurement + lockin_harmonic
    # The default VI's params render (delta-mode).
    assert procedure_win.findChild(QComboBox, "param_voltmeter_range_V_input") is not None
    assert procedure_win.findChild(QLineEdit, "param_n_readings_input") is not None
    # The selector + params live in ONE Measurement box (not a per-group column);
    # the composite box exists and the params key tracks the selected VI.
    assert procedure_win._params_panel._measurement_box is not None
    assert procedure_win._params_panel._measurement_params_key == "measurement:keithley_delta_mode"
    # The Measurement box is NOT registered as an independent column.
    assert "measurement:keithley_delta_mode" not in procedure_win._params_panel._group_boxes


def test_generic_field_sweep_all_four_columns_visible_no_hscroll(procedure_win, station):
    """At 1280 px, Sweep/System/Measurement/Reading loop fit with no h-scroll.

    This is the geometry regression for the reported bug: the measurement
    params and the rightmost column used to overflow off the right edge behind
    a horizontal scrollbar. Assert the actual on-screen geometry, not just
    widget existence.
    """
    from cryosoft.procedures.field_sweep import FieldSweep

    # scanner_enabled must be set before the form is (re)built for the
    # Reading loop group to render; select_procedure_by_name forces a rebuild
    # even when FieldSweep is already the current selection.
    station.set_scanner_enabled(True)
    procedure_win._params_panel.select_procedure_by_name(FieldSweep.name)
    # Put the (enumerated) route parameter in slot 1 so the pick checkboxes
    # render — the widest state the Reading loop column takes.
    _set_slot_parameter(procedure_win, "loop1_parameter", "switch_matrix.route")
    _settle_at_width(procedure_win, 1280, 800)

    # No horizontal scrollbar is needed for the parameter form.
    assert procedure_win._params_panel._param_scroll.horizontalScrollBar().maximum() == 0

    # The selected VI's first parameter widget is fully inside the viewport.
    first_param = procedure_win.findChild(QLineEdit, "param_n_readings_input")
    assert first_param is not None
    assert _fully_inside_param_viewport(procedure_win, first_param)

    # The first pick checkbox is fully inside the viewport (not off-screen).
    first_pick = procedure_win.findChild(QCheckBox, "param_loop1_pick_Mux-Ch1_input")
    assert first_pick is not None
    assert _fully_inside_param_viewport(procedure_win, first_pick)


def test_generic_field_sweep_switching_vi_swaps_only_measurement_group(procedure_win):
    """Switching the measurement VI swaps the measurement widgets, preserving the rest."""
    from cryosoft.procedures.field_sweep import FieldSweep

    _select_procedure(procedure_win, FieldSweep.name)

    # Capture the sweep axis widget, the System input, and the composite
    # Measurement box + method drop-down BEFORE switching.
    axis_before = procedure_win._params_panel._axis_widget
    temp_before = procedure_win.findChild(QLineEdit, "param_temperature_input")
    box_before = procedure_win._params_panel._measurement_box
    combo_before = _measurement_combo(procedure_win)
    assert temp_before is not None and box_before is not None

    _select_measurement(procedure_win, "dc_measurement")

    # The DC VI's params are now present; the delta-only params are gone.
    assert procedure_win.findChild(QLineEdit, "param_readings_per_point_input") is not None
    assert procedure_win.findChild(QLineEdit, "param_n_readings_input") is None
    assert procedure_win.findChild(QComboBox, "param_voltmeter_range_V_input") is None  # dc's is a line edit
    assert procedure_win.findChild(QLineEdit, "param_voltmeter_range_V_input") is not None
    assert procedure_win._params_panel._measurement_params_key == "measurement:dc_measurement"

    # Only the params sub-form was rebuilt: the Measurement box, its method
    # drop-down, the sweep axis widget, and the System input are SAME instances.
    assert procedure_win._params_panel._measurement_box is box_before
    assert _measurement_combo(procedure_win) is combo_before
    assert procedure_win._params_panel._axis_widget is axis_before
    assert procedure_win.findChild(QLineEdit, "param_temperature_input") is temp_before


def test_generic_field_sweep_collect_merges_params_for_both_selections(procedure_win):
    """_collect_params returns the right merged params for each measurement VI."""
    from cryosoft.procedures.field_sweep import FieldSweep

    _select_procedure(procedure_win, FieldSweep.name)

    # Default (delta) selection.
    values, *_ = procedure_win._collect_params()
    assert values["measurement_vi"] == "keithley_delta_mode"
    assert "n_readings" in values and "current" in values
    assert "temperature" in values  # system param present too

    # Switch to DC and collect again.
    _select_measurement(procedure_win, "dc_measurement")
    values2, *_ = procedure_win._collect_params()
    assert values2["measurement_vi"] == "dc_measurement"
    assert "readings_per_point" in values2 and "current_A" in values2
    assert "n_readings" not in values2  # delta-only param is gone


def test_generic_field_sweep_typed_values_survive_selection_round_trip(procedure_win):
    """A value typed under one measurement VI is restored after switching away and back."""
    from cryosoft.procedures.field_sweep import FieldSweep

    _select_procedure(procedure_win, FieldSweep.name)

    # Type a distinctive value into the delta VI's n_readings field.
    n_field = procedure_win.findChild(QLineEdit, "param_n_readings_input")
    n_field.setText("37")

    # Switch to DC, then back to delta.
    _select_measurement(procedure_win, "dc_measurement")
    _select_measurement(procedure_win, "keithley_delta_mode")

    restored = procedure_win.findChild(QLineEdit, "param_n_readings_input")
    assert restored is not None
    assert restored.text() == "37"


def test_generic_field_sweep_renders_route_pick_checkboxes(procedure_win, station):
    """Putting the route in a loop slot renders one pick checkbox per route."""
    from cryosoft.procedures.field_sweep import FieldSweep

    station.set_scanner_enabled(True)
    procedure_win._params_panel.select_procedure_by_name(FieldSweep.name)
    assert "reading_loop" in procedure_win._params_panel._group_boxes

    _set_slot_parameter(procedure_win, "loop1_parameter", "switch_matrix.route")
    for route in ("Mux-Ch1", "Mux-Ch2", "Mux-Ch3", "Mux-Ch4"):
        box = procedure_win.findChild(QCheckBox, f"param_loop1_pick_{route}_input")
        assert box is not None, f"pick checkbox for {route} should render"
        assert box.isChecked() is False  # default False


def test_generic_field_sweep_collect_returns_pick_bools(procedure_win, station):
    """_collect_params returns the loop1_pick_<route> bools from the checkboxes."""
    from cryosoft.procedures.field_sweep import FieldSweep

    station.set_scanner_enabled(True)
    procedure_win._params_panel.select_procedure_by_name(FieldSweep.name)
    _set_slot_parameter(procedure_win, "loop1_parameter", "switch_matrix.route")

    procedure_win.findChild(QCheckBox, "param_loop1_pick_Mux-Ch1_input").setChecked(True)
    procedure_win.findChild(QCheckBox, "param_loop1_pick_Mux-Ch3_input").setChecked(True)

    values, *_ = procedure_win._collect_params()
    assert values["loop1_parameter"] == "switch_matrix.route"
    assert values["loop1_pick_Mux-Ch1"] is True
    assert values["loop1_pick_Mux-Ch2"] is False
    assert values["loop1_pick_Mux-Ch3"] is True
    assert values["loop1_pick_Mux-Ch4"] is False


def test_generic_field_sweep_method_combo_shows_selector_labels(procedure_win, station):
    """The method drop-down shows the SHORT selector_labels, vi_name in a tooltip.

    The combo used to show "vi_name — display_label" (too long; it forced the
    column wide). It now shows each VI's ``selector_label`` and carries the bare
    ``vi_name`` as a per-item tooltip; the collected value is still the vi_name.
    """
    from cryosoft.procedures.field_sweep import FieldSweep

    _select_procedure(procedure_win, FieldSweep.name)
    combo = _measurement_combo(procedure_win)

    items = [combo.itemText(i) for i in range(combo.count())]
    expected = [
        station.measurement_selector_label(n)
        for n in station.measurement_vi_names()
    ]
    assert items == expected
    assert items == [
        "Delta mode (6221 + 2182A)", "DC (6221 + 2182A)", "Lock-in 1f/2f (internal source)",
    ]

    # Each item carries its vi_name as a tooltip (disambiguation).
    tips = [
        combo.itemData(i, Qt.ItemDataRole.ToolTipRole)
        for i in range(combo.count())
    ]
    assert tips == station.measurement_vi_names()

    # The collected value stays the vi_name, not the label.
    values, *_ = procedure_win._collect_params()
    assert values["measurement_vi"] == "keithley_delta_mode"


def test_generic_field_sweep_pick_row_label_strips_prefix(procedure_win, station):
    """The pick checkbox row label is the bare choice; the key keeps its slot.

    Only the visible label is prettified — the parameter key (and thus the
    HDF5 metadata key) remains ``loop1_pick_<value>`` so choices stay
    namespaced per slot.
    """
    from cryosoft.procedures.field_sweep import FieldSweep

    station.set_scanner_enabled(True)
    procedure_win._params_panel.select_procedure_by_name(FieldSweep.name)
    _set_slot_parameter(procedure_win, "loop1_parameter", "switch_matrix.route")

    checkbox = procedure_win.findChild(QCheckBox, "param_loop1_pick_Mux-Ch1_input")
    assert checkbox is not None
    form = checkbox.parent().layout()
    assert isinstance(form, QFormLayout)
    row_label = form.labelForField(checkbox)
    assert isinstance(row_label, QLabel)
    assert row_label.text() == "Mux-Ch1:"  # bare choice, no slot prefix

    checkbox.setChecked(True)
    values, *_ = procedure_win._collect_params()
    assert values["loop1_pick_Mux-Ch1"] is True  # prefixed key kept
    assert "Mux-Ch1" not in values               # bare choice is not a key


def test_live_plot_loop_selectors_follow_reading_loop(procedure_win, station):
    """The plots' two Loop selectors follow the reading-loop slots.

    Hidden while nothing is loopable (delta VI, scanner off); visible but
    disabled once something is loopable with the slots off; enabled with one
    item per value — display text carrying the value, item data carrying the
    index label — once a slot has two or more values.
    """
    from cryosoft.procedures.field_sweep import FieldSweep

    procedure_win._params_panel.select_procedure_by_name(FieldSweep.name)
    sel1 = procedure_win.findChild(QComboBox, "plot1_loop1_selector")
    sel2 = procedure_win.findChild(QComboBox, "plot1_loop2_selector")
    assert sel1 is not None and sel2 is not None
    # Default: delta VI, scanner off -> nothing loopable, selectors hidden.
    assert not sel1.isVisibleTo(procedure_win)
    assert not sel2.isVisibleTo(procedure_win)

    # Enable the scanner and rebuild: the route is loopable now, slots off ->
    # both selectors visible but disabled.
    station.set_scanner_enabled(True)
    procedure_win._params_panel.select_procedure_by_name(FieldSweep.name)
    sel1 = procedure_win.findChild(QComboBox, "plot1_loop1_selector")
    sel2 = procedure_win.findChild(QComboBox, "plot1_loop2_selector")
    assert sel1.isVisibleTo(procedure_win) and not sel1.isEnabled()
    assert sel2.isVisibleTo(procedure_win) and not sel2.isEnabled()

    # Slot 1: route with two channels ticked. Slot 2: DC current +/- pair.
    _select_measurement(procedure_win, "dc_measurement")
    _set_slot_parameter(procedure_win, "loop1_parameter", "switch_matrix.route")
    procedure_win.findChild(QCheckBox, "param_loop1_pick_Mux-Ch1_input").setChecked(True)
    procedure_win.findChild(QCheckBox, "param_loop1_pick_Mux-Ch2_input").setChecked(True)
    _set_slot_parameter(
        procedure_win, "loop2_parameter", "dc_measurement.current_A"
    )
    values_edit = procedure_win.findChild(QLineEdit, "param_loop2_values_input")
    values_edit.setText("1e-6, -1e-6")
    values_edit.editingFinished.emit()

    sel1 = procedure_win.findChild(QComboBox, "plot1_loop1_selector")
    sel2 = procedure_win.findChild(QComboBox, "plot1_loop2_selector")
    assert sel1.isEnabled()
    assert [sel1.itemText(i) for i in range(sel1.count())] == [
        "A1 = Mux-Ch1", "A2 = Mux-Ch2",
    ]
    assert [sel1.itemData(i) for i in range(sel1.count())] == ["A1", "A2"]
    assert sel2.isEnabled()
    assert [sel2.itemText(i) for i in range(sel2.count())] == [
        "B1 = 1e-06", "B2 = -1e-06",
    ]
    # Axis keys stay plain — the Loop selectors pick the reading.
    x_sel = procedure_win.findChild(QComboBox, "x1_axis_selector")
    keys = [x_sel.itemText(i) for i in range(x_sel.count())]
    assert "voltage_V" in keys
    assert not any("__A" in k or "__B" in k for k in keys)


def test_param_form_renders_all_widget_kinds_and_round_trips(qtbot):
    """param_form maps each ParamSpec kind to the right widget and round-trips values.

    Exercises cryosoft.gui.param_form directly (no ProcedureWindow) on a
    synthetic ParamGroup covering the four shapes: plain float, bounded int,
    enumerated choices, and bool.
    """
    from cryosoft.core.plan import ParamGroup, ParamSpec
    from cryosoft.gui import param_form

    group = ParamGroup(
        key="demo",
        title="Demo",
        params={
            "amp": ParamSpec(type=float, default=1.5, unit="V", description="Amplitude"),
            "count": ParamSpec(type=int, default=4, min=1, max=10, description="Count"),
            "range": ParamSpec(
                type=float,
                default=0.1,
                choices={"0.1 V": 0.1, "1 V": 1.0},
                description="Range",
            ),
            "enabled": ParamSpec(type=bool, default=True, description="Enabled"),
        },
    )
    box, widgets = param_form.build_group_box(group)
    qtbot.addWidget(box)

    assert box.title() == "Demo"
    assert isinstance(widgets["amp"], QLineEdit)
    assert isinstance(widgets["count"], QLineEdit)
    assert isinstance(widgets["range"], QComboBox)
    assert isinstance(widgets["enabled"], QCheckBox)

    # objectName convention preserved (findChild API relied on by other tests).
    assert widgets["amp"].objectName() == "param_amp_input"

    # Defaults are seeded onto the widgets.
    assert widgets["amp"].text() == "1.5"
    assert widgets["range"].currentText() == "0.1 V"   # label whose value is 0.1
    assert widgets["enabled"].isChecked() is True

    # collect_value reverses the mapping on the seeded defaults.
    assert param_form.collect_value(widgets["amp"], group.params["amp"]) == pytest.approx(1.5)
    assert param_form.collect_value(widgets["count"], group.params["count"]) == 4
    assert param_form.collect_value(widgets["range"], group.params["range"]) == pytest.approx(0.1)
    assert param_form.collect_value(widgets["enabled"], group.params["enabled"]) is True

    # Change values, re-collect: text parses by type, combobox maps label->value.
    widgets["count"].setText("7")
    widgets["range"].setCurrentText("1 V")
    widgets["enabled"].setChecked(False)
    assert param_form.collect_value(widgets["count"], group.params["count"]) == 7
    assert param_form.collect_value(widgets["range"], group.params["range"]) == pytest.approx(1.0)
    assert param_form.collect_value(widgets["enabled"], group.params["enabled"]) is False

    # Raw string round-trip used by the session cache.
    param_form.set_widget_raw(widgets["amp"], "2.5")
    assert param_form.get_widget_raw(widgets["amp"]) == "2.5"


def test_monitor_sample_info_inputs_exist(monitor_win):
    """Sample name, ID, and comments fields are present in MonitorWindow."""
    assert monitor_win._session_info._sample_name_input is not None
    assert monitor_win._session_info._sample_id_input is not None
    assert monitor_win._session_info._comments_input is not None


def test_monitor_data_dir_input_exists(monitor_win):
    """Data directory input is in MonitorWindow and has a default value."""
    assert monitor_win._session_info._data_dir_input is not None
    assert monitor_win._session_info._data_dir_input.text() != ""


def test_monitor_get_sample_info(monitor_win):
    """get_sample_info() returns a dict with the correct keys."""
    info = monitor_win.get_sample_info()
    assert "sample_name" in info
    assert "sample_id" in info
    assert "comments" in info


def test_monitor_get_data_dir(monitor_win):
    """get_data_dir() returns a non-empty string."""
    assert monitor_win.get_data_dir() != ""


# ── Experiment lifecycle (SessionInfoPanel) ────────────────────────────────────

@pytest.fixture
def session_manager(tmp_path, station, orchestrator):
    """SessionManager backed by a tmp_path store/roster, with one roster user."""
    from cryosoft.session.manager import SessionManager
    from cryosoft.session.models import User
    from cryosoft.session.store import ExperimentStore, UserRoster

    roster = UserRoster(tmp_path / "users.json")
    roster.add(User(user_id="jdoe", name="J. Doe", email="jdoe@example.org"))
    store = ExperimentStore(tmp_path / "experiments")
    return SessionManager(
        store=store,
        roster=roster,
        orchestrator=orchestrator,
        station=station,
        config_name="sim_cryostat",
    )


@pytest.fixture
def monitor_win_session(station, orchestrator, session_manager, qtbot):
    """MonitorWindow wired to a real SessionManager."""
    win = MonitorWindow(station, orchestrator, session_manager=session_manager)
    qtbot.addWidget(win)
    win.show()
    return win


class _FakeStartDialog:
    """Stand-in for StartExperimentDialog that auto-accepts fixed values."""

    def __init__(self, values: tuple[str, str, bool]) -> None:
        self._values = values

    def exec(self):
        return QDialog.DialogCode.Accepted

    def result_values(self):
        return self._values


class _FakeCloseDialog:
    """Stand-in for CloseExperimentDialog that auto-accepts fixed findings."""

    def __init__(self, findings_text: str) -> None:
        self._findings_text = findings_text

    def exec(self):
        return QDialog.DialogCode.Accepted

    def findings(self):
        return self._findings_text


def _stub_start_dialog(monkeypatch, title, user_id, attended=True):
    """Replace StartExperimentDialog with a fake that auto-accepts ``values``."""
    from cryosoft.gui import session_info_panel as sip

    monkeypatch.setattr(
        sip,
        "StartExperimentDialog",
        lambda roster, parent=None: _FakeStartDialog((title, user_id, attended)),
    )


def _stub_close_dialog(monkeypatch, findings_text=""):
    """Replace CloseExperimentDialog with a fake that auto-accepts ``findings_text``."""
    from cryosoft.gui import session_info_panel as sip

    monkeypatch.setattr(
        sip,
        "CloseExperimentDialog",
        lambda current_findings="", parent=None: _FakeCloseDialog(findings_text),
    )


def test_experiment_row_disabled_without_session_manager(monitor_win):
    """The Start Experiment button is disabled when no SessionManager is wired."""
    btn = monitor_win._session_info._start_close_btn
    assert not btn.isEnabled()
    assert monitor_win._session_info._experiment_status_label.text() == "No experiment open"


def test_experiment_row_enabled_with_session_manager(monitor_win_session):
    """The Start Experiment button is enabled and shows the closed state."""
    panel = monitor_win_session._session_info
    assert panel._start_close_btn.isEnabled()
    assert panel._start_close_btn.text() == "Start Experiment…"
    assert not panel._attended_checkbox.isVisible()


def test_start_experiment_updates_panel_and_manager(monitor_win_session, session_manager, monkeypatch):
    """Clicking Start Experiment opens the dialog and installs the experiment."""
    _stub_start_dialog(monkeypatch, "Hall bar A3", "jdoe", attended=True)
    panel = monitor_win_session._session_info

    panel._start_close_btn.click()

    experiment = session_manager.current_experiment()
    assert experiment is not None
    assert experiment.title == "Hall bar A3"
    assert experiment.user_id == "jdoe"
    assert panel._start_close_btn.text() == "Close Experiment…"
    assert "Hall bar A3" in panel._experiment_status_label.text()
    assert "J. Doe" in panel._experiment_status_label.text()
    assert panel._attended_checkbox.isVisible()
    assert panel._attended_checkbox.isChecked()
    assert "not configured" in panel._eln_status_label.text()


def test_eln_status_shows_published_url_when_eln_link_set(
    monitor_win_session, session_manager, monkeypatch
):
    """Once ElnLink carries a url, the panel reflects it instead of the placeholder."""
    from cryosoft.session.models import ElnLink

    _stub_start_dialog(monkeypatch, "Hall bar A3", "jdoe")
    panel = monitor_win_session._session_info
    panel._start_close_btn.click()

    experiment = session_manager.current_experiment()
    experiment.eln_link = ElnLink(backend="elabftw", entry_id="42", url="https://elab.example/42")
    session_manager.experiment_changed.emit(experiment.to_dict())

    assert panel._eln_status_label.text() == "Published: https://elab.example/42"


def test_close_experiment_saves_findings_and_resets_panel(
    monitor_win_session, session_manager, monkeypatch
):
    """Clicking Close Experiment saves findings and reverts the panel to closed."""
    _stub_start_dialog(monkeypatch, "Hall bar A3", "jdoe")
    panel = monitor_win_session._session_info
    panel._start_close_btn.click()
    experiment_id = session_manager.current_experiment().experiment_id

    _stub_close_dialog(monkeypatch, "Saw a clean switching signal.")
    panel._start_close_btn.click()

    assert session_manager.current_experiment() is None
    assert panel._start_close_btn.text() == "Start Experiment…"
    assert panel._experiment_status_label.text() == "No experiment open"
    assert not panel._attended_checkbox.isVisible()
    assert "not configured" in panel._eln_status_label.text()
    closed = session_manager.store.load(experiment_id)
    assert closed.findings == "Saw a clean switching signal."
    assert closed.status == "closed"


def test_attendance_checkbox_toggle_calls_set_attended(
    monitor_win_session, session_manager, monkeypatch
):
    """Unchecking Attended flips the experiment's attendance flag."""
    _stub_start_dialog(monkeypatch, "Hall bar A3", "jdoe", attended=True)
    panel = monitor_win_session._session_info
    panel._start_close_btn.click()

    panel._attended_checkbox.setChecked(False)

    assert session_manager.current_experiment().attended is False


def test_add_user_dialog_autofills_id_from_name(qtbot):
    """AddUserDialog derives a roster-key slug from the typed name."""
    from cryosoft.gui.experiment_dialogs import AddUserDialog

    dialog = AddUserDialog()
    qtbot.addWidget(dialog)
    dialog._name_input.setText("Jane O'Doe")

    assert dialog._id_input.text() == "jane_o_doe"
    user = dialog.user()
    assert user.user_id == "jane_o_doe"
    assert user.name == "Jane O'Doe"


# ── Setup tier: login and instrument info (User / Config menus) ───────────────

def test_current_user_label_defaults_not_logged_in(monitor_win):
    """With nobody logged in, the header shows the default state."""
    assert monitor_win._current_user_label.text() == "Not logged in"


def test_login_dialog_lists_roster_users(qtbot, tmp_path):
    """LoginDialog's user picker is pre-populated from the roster."""
    from cryosoft.gui.setup_dialogs import LoginDialog
    from cryosoft.session.models import User
    from cryosoft.session.store import UserRoster

    roster = UserRoster(tmp_path / "users.json")
    roster.add(User(user_id="jdoe", name="J. Doe"))

    dialog = LoginDialog(roster)
    qtbot.addWidget(dialog)

    assert dialog._user_picker.has_users()
    assert dialog._user_picker.selected_user_id() == "jdoe"


def test_open_login_dialog_without_session_manager_shows_message(monitor_win, monkeypatch):
    """No SessionManager wired: the login action informs rather than crashing."""
    shown = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a))
    monitor_win._open_login_dialog()
    assert shown


def test_switch_user_saves_outgoing_and_loads_incoming_session(
    station, orchestrator, qtbot, tmp_path
):
    """_switch_user() persists the outgoing user's fields and loads the incoming one's."""
    from cryosoft.gui import app_settings as _app_settings
    from cryosoft.session.manager import SessionManager
    from cryosoft.session.models import User
    from cryosoft.session.store import ExperimentStore, UserRoster

    roster = UserRoster(tmp_path / "users.json")
    roster.add(User(user_id="jdoe", name="J. Doe"))
    roster.add(User(user_id="asmith", name="A. Smith"))
    manager = SessionManager(
        store=ExperimentStore(tmp_path / "experiments"),
        roster=roster,
        orchestrator=orchestrator,
        station=station,
        config_name="sim_cryostat",
    )
    win = MonitorWindow(station, orchestrator, session_manager=manager)
    qtbot.addWidget(win)
    win.show()

    win._switch_user("jdoe")
    assert win._current_user_id == "jdoe"
    assert win._current_user_label.text() == "Logged in as J. Doe"
    assert _app_settings.current_user_id() == "jdoe"
    assert win._session_info._sample_name_input.text() == ""  # jdoe's file is fresh

    win._session_info._sample_name_input.setText("SampleB")
    win._switch_user("asmith")
    assert win._current_user_label.text() == "Logged in as A. Smith"
    assert win._session_info._sample_name_input.text() == ""  # asmith's file is fresh

    win._switch_user("jdoe")
    assert win._session_info._sample_name_input.text() == "SampleB"


def test_open_login_dialog_full_flow(station, orchestrator, qtbot, tmp_path, monkeypatch):
    """Confirming LoginDialog switches the current user."""
    from cryosoft.gui import monitor_window as mw
    from cryosoft.session.manager import SessionManager
    from cryosoft.session.models import User
    from cryosoft.session.store import ExperimentStore, UserRoster

    roster = UserRoster(tmp_path / "users.json")
    roster.add(User(user_id="jdoe", name="J. Doe"))
    manager = SessionManager(
        store=ExperimentStore(tmp_path / "experiments"),
        roster=roster,
        orchestrator=orchestrator,
        station=station,
    )
    win = MonitorWindow(station, orchestrator, session_manager=manager)
    qtbot.addWidget(win)
    win.show()

    class _FakeLoginDialog:
        DialogCode = QDialog.DialogCode

        def __init__(self, *a, **k):
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted

        def selected_user_id(self):
            return "jdoe"

    monkeypatch.setattr(mw, "LoginDialog", _FakeLoginDialog)
    win._open_login_dialog()

    assert win._current_user_id == "jdoe"
    assert win._current_user_label.text() == "Logged in as J. Doe"


def test_instrument_info_action_opens_dialog_with_config_metadata(
    station, orchestrator, qtbot, monkeypatch
):
    """Config menu's Instrument Info… reads read_instrument_metadata() for the active config."""
    from cryosoft.gui import monitor_window as mw

    win = MonitorWindow(
        station, orchestrator, active_config_path="cryosoft/configs/sim_cryostat"
    )
    qtbot.addWidget(win)
    win.show()

    captured = {}

    class _FakeInstrumentInfoDialog:
        def __init__(self, metadata, parent=None):
            captured["metadata"] = metadata

        def exec(self):
            return None

    monkeypatch.setattr(mw, "InstrumentInfoDialog", _FakeInstrumentInfoDialog)
    win._open_instrument_info()

    assert captured["metadata"]["magnet_x"]["role"] == "X-axis magnet"


def test_instrument_info_dialog_handles_empty_metadata(qtbot):
    """An empty metadata dict shows the fallback message instead of an empty scroll area."""
    from cryosoft.gui.setup_dialogs import InstrumentInfoDialog

    dialog = InstrumentInfoDialog({})
    qtbot.addWidget(dialog)
    assert dialog.findChild(QScrollArea, "instrument_info_scroll") is None


def test_procedure_control_buttons_exist(procedure_win, qtbot):
    """Pause, Resume, Abort buttons are present."""
    from PyQt6.QtWidgets import QPushButton
    assert procedure_win.findChild(QPushButton, "pause_btn") is not None
    assert procedure_win.findChild(QPushButton, "resume_btn") is not None
    assert procedure_win.findChild(QPushButton, "abort_btn") is not None


def test_ack_button_hidden_when_not_emergency(procedure_win):
    """Emergency acknowledge button is hidden in normal state."""
    assert not procedure_win._ack_btn.isVisible()


def test_ack_button_visible_in_emergency(procedure_win, orchestrator):
    """Emergency acknowledge button appears when EMERGENCY state is emitted."""
    orchestrator.state_changed.emit(OrchestratorState.EMERGENCY.value)
    assert procedure_win._ack_btn.isVisible()

    # Disappears on acknowledge
    orchestrator.state_changed.emit(OrchestratorState.IDLE.value)
    assert not procedure_win._ack_btn.isVisible()


def test_progress_bar_updates(procedure_win, orchestrator):
    """Progress bar reflects procedure_progress signal."""
    orchestrator.procedure_progress.emit(0.42)
    assert procedure_win._progress_bar.value() == 42


def test_add_to_queue_appends_item(procedure_win, qtbot):
    """Add to Queue populates the queue list widget."""
    initial_count = procedure_win._queue_panel._queue_list.count()
    qtbot.mouseClick(
        procedure_win.findChild(QPushButton, "add_to_queue_btn"),
        __import__("PyQt6.QtCore", fromlist=["Qt"]).Qt.MouseButton.LeftButton,
    )
    assert procedure_win._queue_panel._queue_list.count() == initial_count + 1


def test_file_prefix_input_exists(procedure_win):
    """The filename-prefix field is present above the parameter form."""
    from PyQt6.QtWidgets import QLineEdit

    assert procedure_win.findChild(QLineEdit, "file_prefix_input") is not None


def test_add_to_queue_captures_current_file_prefix(procedure_win, qtbot):
    """Each queue entry freezes the file-prefix field's value at add-time."""
    add_btn = procedure_win.findChild(QPushButton, "add_to_queue_btn")
    Qt = __import__("PyQt6.QtCore", fromlist=["Qt"]).Qt

    procedure_win._params_panel._file_prefix_input.setText("run_a")
    qtbot.mouseClick(add_btn, Qt.MouseButton.LeftButton)

    procedure_win._params_panel._file_prefix_input.setText("run_b")
    qtbot.mouseClick(add_btn, Qt.MouseButton.LeftButton)

    prefixes = [entry.file_prefix for entry in procedure_win._queue_panel._queue]
    assert prefixes[-2:] == ["run_a", "run_b"]
    assert "run_a" in procedure_win._queue_panel._queue_list.item(len(prefixes) - 2).text()
    assert "run_b" in procedure_win._queue_panel._queue_list.item(len(prefixes) - 1).text()


def test_blank_file_prefix_omitted_from_queue_label(procedure_win, qtbot):
    """A blank prefix leaves the queue label as just the procedure name."""
    add_btn = procedure_win.findChild(QPushButton, "add_to_queue_btn")
    Qt = __import__("PyQt6.QtCore", fromlist=["Qt"]).Qt

    procedure_win._params_panel._file_prefix_input.setText("")
    qtbot.mouseClick(add_btn, Qt.MouseButton.LeftButton)

    entry = procedure_win._queue_panel._queue[-1]
    assert entry.file_prefix == ""
    assert "[" not in procedure_win._queue_panel._queue_list.item(procedure_win._queue_panel._queue_list.count() - 1).text()
    assert entry.cls.name in procedure_win._queue_panel._queue_list.item(procedure_win._queue_panel._queue_list.count() - 1).text()


def test_run_now_passes_file_prefix_to_procedure_instance(procedure_win, qtbot):
    """Run Now builds a procedure carrying the current file-prefix field value."""
    procedure_win._params_panel._file_prefix_input.setText("live_run")
    proc = procedure_win._build_procedure_instance()
    assert proc is not None
    assert proc._file_prefix == "live_run"


def test_measurement_ready_updates_plot(procedure_win, orchestrator):
    """measurement_ready signal appends the datapoint to _datapoints."""
    datapoint = {"field_T": 0.5, "voltage_V": [1.23e-6] * 10}
    orchestrator.measurement_ready.emit(datapoint)

    assert len(procedure_win._datapoints) == 1
    assert abs(procedure_win._datapoints[0]["field_T"] - 0.5) < 1e-9


# ── Fixed 2x2 quadrant layout tests (GUI optimization redesign) ─────────────

def test_monitor_quadrant_splitters_not_collapsible(monitor_win):
    """The 3 quadrant splitters exist, resizable (not collapsible), correctly oriented."""
    assert monitor_win._main_splitter.orientation() == Qt.Orientation.Horizontal
    assert monitor_win._left_splitter.orientation() == Qt.Orientation.Vertical
    assert monitor_win._right_splitter.orientation() == Qt.Orientation.Vertical
    for splitter in (monitor_win._main_splitter, monitor_win._left_splitter, monitor_win._right_splitter):
        assert splitter.childrenCollapsible() is False
        assert splitter.count() == 2


def test_monitor_instrument_panels_exist_for_system_vis(monitor_win, station):
    """One InstrumentPanel exists per system/level VI, in the top-left quadrant."""
    system_vis = [
        n for n in station.get_vi_names() if station.get_vi_type(n) in {"system", "level"}
    ]
    assert system_vis, "sim_cryostat should have at least one system/level VI"
    panel_vi_names = {p._vi_name for p in monitor_win._panels}
    assert panel_vi_names == set(system_vis)
    for panel in monitor_win._panels:
        assert isinstance(panel, InstrumentPanel)


def test_monitor_fixed_quadrants_exist_with_expected_content(monitor_win):
    """Sample Info and the Other Devices/Log stack contain the expected widgets."""
    sample_quadrant = monitor_win.findChild(QScrollArea, "session_info_scroll")
    assert sample_quadrant is not None
    assert sample_quadrant.widget().findChild(QLineEdit, "sample_name_input") is not None

    stack = monitor_win.findChild(QStackedWidget, "devices_log_stack")
    assert stack is not None
    assert stack.count() == 2
    assert stack.widget(1) is monitor_win._log_panel


def test_monitor_default_trend_panels_exist_and_gridded(monitor_win):
    """Two trend panels exist by default, each placed in the trends QGridLayout."""
    assert len(monitor_win._trends._trend_panels) == 2
    for panel in monitor_win._trends._trend_panels.values():
        assert monitor_win._trends._trends_grid.indexOf(panel) != -1


def test_monitor_has_no_view_menu(monitor_win):
    """Nothing in the fixed quadrant layout can be hidden/closed, so there is no View menu.

    The Session menu (state management) is a separate, always-present menu; the
    point preserved here is that the dock-era View menu stays gone.
    """
    menu_titles = {action.text() for action in monitor_win.menuBar().actions()}
    assert "View" not in menu_titles
    assert "Procedures" in menu_titles


def test_monitor_trends_grid_arranges_in_ceil_sqrt_grid(monitor_win):
    """Adding trend plots up to the cap of 4 arranges them in a 2x2 grid, not a stack."""
    monitor_win._trends._add_trend_panel()  # 3rd panel: ceil(sqrt(3)) = 2 columns
    monitor_win._trends._add_trend_panel()  # 4th panel: ceil(sqrt(4)) = 2 columns
    assert len(monitor_win._trends._trend_panels) == 4

    positions = {
        monitor_win._trends._trends_grid.getItemPosition(i)[:2]
        for i in range(monitor_win._trends._trends_grid.count())
    }
    assert positions == {(0, 0), (0, 1), (1, 0), (1, 1)}


def test_monitor_add_trend_plot_button_caps_at_four(monitor_win):
    """The Trends quadrant's Add button adds panels up to 4, then disables and stays inert."""
    assert len(monitor_win._trends._trend_panels) == 2
    assert monitor_win._trends._add_trend_btn.isEnabled()

    monitor_win._trends._add_trend_btn.click()
    assert len(monitor_win._trends._trend_panels) == 3
    monitor_win._trends._add_trend_btn.click()
    assert len(monitor_win._trends._trend_panels) == 4
    assert not monitor_win._trends._add_trend_btn.isEnabled()

    monitor_win._trends._add_trend_btn.click()
    assert len(monitor_win._trends._trend_panels) == 4


def test_monitor_trend_remove_button_drops_panel_never_below_one(monitor_win):
    """The panel's own remove button destroys the panel, stopping at a floor of 1."""
    assert len(monitor_win._trends._trend_panels) == 2
    first_id = next(iter(monitor_win._trends._trend_panels))

    monitor_win._trends._on_trend_remove_requested(first_id)
    assert len(monitor_win._trends._trend_panels) == 1
    assert first_id not in monitor_win._trends._trend_panels

    remaining_id = next(iter(monitor_win._trends._trend_panels))
    monitor_win._trends._on_trend_remove_requested(remaining_id)
    assert len(monitor_win._trends._trend_panels) == 1  # floor holds

    assert monitor_win._trends._add_trend_btn.isEnabled()


def test_monitor_other_devices_log_selector_switches_stack(monitor_win):
    """Changing the View selector switches the bottom-right stacked widget's page."""
    assert monitor_win._devices_log_stack.currentIndex() == 0
    monitor_win._devices_log_selector.setCurrentIndex(1)
    assert monitor_win._devices_log_stack.currentIndex() == 1
    monitor_win._devices_log_selector.setCurrentIndex(0)
    assert monitor_win._devices_log_stack.currentIndex() == 0


def test_monitor_other_devices_lists_switch_vi_with_display_label(monitor_win, station):
    """The Other Devices section shows a display-only row for each switch VI.

    The sim_cryostat config has one switch VI (switch_matrix, a Keithley-705
    scanner behind SwitchMatrixVI). Its row must carry the VI's display_label
    ("Scanner (mux)") and a connection dot, but no Check/lifecycle buttons —
    it is monitored, not driven, from this section.
    """
    switch_vis = [n for n in station.get_vi_names() if station.get_vi_type(n) == "switch"]
    assert switch_vis, "sim_cryostat should have at least one switch VI"

    for vi_name in switch_vis:
        row = monitor_win.findChild(QWidget, f"{vi_name}_switch_row")
        assert row is not None, f"Missing switch row for {vi_name}"

        label = monitor_win.findChild(QLabel, f"{vi_name}_display_label")
        assert label is not None
        assert label.text() == station.measurement_label(vi_name)

        assert monitor_win.findChild(QLabel, f"{vi_name}_conn_dot") is not None
        assert monitor_win.findChild(QLabel, f"{vi_name}_active_route") is not None
        # Display-only: no Check button, no lifecycle toggle.
        assert monitor_win.findChild(QPushButton, f"{vi_name}_check_btn") is None
        assert monitor_win.findChild(QPushButton, f"{vi_name}_lifecycle_btn") is None


def test_monitor_switch_row_active_route_updates_on_tick(monitor_win, station, orchestrator):
    """A switch row's active-route label tracks the live route across a monitor tick.

    Selecting a route on the sim station and emitting the resulting state
    snapshot (as the Orchestrator tick does) must flip the label from the
    no-route placeholder to the closed route's name, and mark the row
    connected.
    """
    route_lbl = monitor_win.findChild(QLabel, "switch_matrix_active_route")
    dot = monitor_win.findChild(QLabel, "switch_matrix_conn_dot")
    assert route_lbl.text() == "Route: —"

    station.switch_matrix.select_route("Mux-Ch2")
    orchestrator.states_updated.emit(station.get_state())

    assert route_lbl.text() == "Route: Mux-Ch2"
    assert dot.property("status") == "connected"

    station.switch_matrix.open_all()
    orchestrator.states_updated.emit(station.get_state())
    assert route_lbl.text() == "Route: —"


def test_monitor_states_updated_feeds_history_and_trend_combos(monitor_win, orchestrator):
    """states_updated records into MonitorHistory and populates the trend Y combos."""
    fake_state = {"magnet_x": {"get_field": 0.25, "magnet_current": 12.0}}
    orchestrator.states_updated.emit(fake_state)

    assert "magnet_x_get_field" in monitor_win._trends._history.keys()

    panels = monitor_win.findChildren(TrendPlotPanel)
    assert len(panels) == 2
    for panel in panels:
        combo = panel.findChild(QComboBox)
        assert combo is not None
        assert combo.count() > 0


def test_monitor_default_trend_key_hints_prefer_readings_over_settings(monitor_win, orchestrator):
    """The two default trend docks pick a temperature/level READING, not a setting/rate field.

    Regression pin: a plain substring search for "temperature"/"level" matches
    the VI-name prefix on fields like temperature_sample_heater_output or
    level_meter_get_refresh_rate before it reaches the actual reading. Which
    specific VI wins alphabetically is not asserted here (real orchestrator
    ticks may have already populated history for other VIs too) — only that
    the FIELD chosen is the reading, not a setting/rate.
    """
    fake_state = {
        "temperature_vti": {"heater_output": 0.0, "temperature": 4.2, "setpoint": 4.2},
        "level_meter": {"get_refresh_rate": 0.0, "helium_level": 77.0, "nitrogen_level": 88.0},
    }
    orchestrator.states_updated.emit(fake_state)

    trend_0 = monitor_win._trends._trend_panels["trend_0"]
    trend_1 = monitor_win._trends._trend_panels["trend_1"]
    assert trend_0.selected_key().endswith("_temperature")
    assert trend_1.selected_key().endswith(("_helium_level", "_nitrogen_level"))


def test_monitor_persistence_roundtrip_splitters_and_trends(
    station, orchestrator, qtbot, isolated_settings
):
    """Closing persists splitter proportions + trend selections; a fresh window restores them.

    Mirrors the existing geometry-persistence test: build a window, change
    state, close it (persisting via closeEvent to the isolated ini), then
    build a fresh window against the same settings and check the state came
    back. Unlike the old dock-based design, there is no explicit "Save
    layout" action — splitter state persists automatically alongside window
    geometry, the same way it already did for plain window size/position.
    """
    win1 = MonitorWindow(station, orchestrator)
    qtbot.addWidget(win1)
    win1.show()

    third_id = win1._trends._add_trend_panel()
    third_panel = win1._trends._trend_panels[third_id]

    # Feed history AFTER the third panel exists so its refresh() (triggered by
    # this emit) populates its Y combo with a real key to select.
    fake_state = {"magnet_x": {"get_field": 0.5, "magnet_current": 10.0}}
    orchestrator.states_updated.emit(fake_state)
    third_panel.set_selected_key("magnet_x_get_field")
    third_panel.set_selected_window_s(21600.0)  # "6 h"

    assert len(win1._trends._trend_panels) == 3

    win1._main_splitter.setSizes([300, 900])
    win1.close()  # persists geometry + splitter state via closeEvent

    win2 = MonitorWindow(station, orchestrator)
    qtbot.addWidget(win2)
    win2.show()

    assert len(win2._trends._trend_panels) == 3
    # Splitter proportions were restored, not left at the [600, 600] default.
    assert win2._main_splitter.sizes() != [600, 600]

    # Give the new window's (empty) history the same key so the persisted
    # selection, held pending, can actually be applied.
    orchestrator.states_updated.emit(fake_state)

    third_id_2 = list(win2._trends._trend_panels.keys())[2]
    third_panel_2 = win2._trends._trend_panels[third_id_2]
    assert third_panel_2.selected_key() == "magnet_x_get_field"
    assert third_panel_2.selected_window_s() == 21600.0


def test_monitor_default_layout_when_settings_empty(monitor_win, station):
    """With no saved splitter state (fresh isolated settings), the DEFAULT layout stands."""
    system_vis = [
        n for n in station.get_vi_names() if station.get_vi_type(n) in {"system", "level"}
    ]
    assert len(monitor_win._trends._trend_panels) == 2
    assert len(monitor_win._panels) == len(system_vis)  # instrument panels were built and placed
    # setSizes([600, 600]) is a proportional hint, not exact pixels once shown
    # at the real window width — check the default is an even 50/50 split.
    left, right = monitor_win._main_splitter.sizes()
    assert abs(left - right) <= 2


def test_procedure_splitters_not_collapsible(procedure_win):
    """All ProcedureWindow splitters have children-collapsing disabled.

    Four splitters: the main horizontal split, the left/right vertical
    quadrant splits, and the queue-over-status vertical split inside the
    top-right quadrant.
    """
    splitters = procedure_win.findChildren(QSplitter)
    assert len(splitters) == 4, f"Expected 4 splitters, found {len(splitters)}"
    for sp in splitters:
        assert sp.childrenCollapsible() is False


def test_status_log_present_and_read_only(procedure_win):
    """The concise Status log widget exists in the top-right quadrant and is read-only."""
    status_log = procedure_win.findChild(QTextEdit, "status_log")
    assert status_log is not None, "status_log not found"
    assert status_log.isReadOnly()


def test_status_log_appends_status_messages(procedure_win, orchestrator):
    """A status_message signal appends a timestamped line to the Status log."""
    status_log = procedure_win.findChild(QTextEdit, "status_log")
    orchestrator.status_message.emit("Measuring point 3/11")
    assert "Measuring point 3/11" in status_log.toPlainText()


def test_procedure_quadrant_splitters_correctly_oriented(procedure_win):
    """main_splitter is horizontal; left/right splitters (params/plot1, queue/plot2) are vertical."""
    assert procedure_win._main_splitter.orientation() == Qt.Orientation.Horizontal
    assert procedure_win._left_splitter.orientation() == Qt.Orientation.Vertical
    assert procedure_win._right_splitter.orientation() == Qt.Orientation.Vertical
    assert procedure_win._left_splitter.widget(0).objectName() == "params_quadrant"
    assert procedure_win._left_splitter.widget(1) is procedure_win._plot1
    assert procedure_win._right_splitter.widget(0).objectName() == "queue_quadrant"
    assert procedure_win._right_splitter.widget(1) is procedure_win._plot2


def test_procedure_param_scroll_has_no_height_cap(procedure_win):
    """The parameter scroll area fills its quadrant instead of being capped at a fixed height."""
    assert procedure_win._params_panel._param_scroll.maximumHeight() >= 16777215  # Qt's QWIDGETSIZE_MAX default (uncapped)


def test_monitor_central_widget_not_scroll_area(monitor_win):
    """The central widget is the content widget directly, holding the main quadrant splitter."""
    assert not isinstance(monitor_win.centralWidget(), QScrollArea)
    assert monitor_win.centralWidget().findChild(QSplitter, "main_splitter") is monitor_win._main_splitter


def test_log_handler_removed_on_close(station, orchestrator, qtbot):
    """Closing MonitorWindow detaches its log handler from the cryosoft logger."""
    win = MonitorWindow(station, orchestrator)
    qtbot.addWidget(win)
    handler = win._log_panel.handler
    cryosoft_logger = logging.getLogger("cryosoft")
    assert handler in cryosoft_logger.handlers

    win.close()
    assert handler not in cryosoft_logger.handlers


def test_monitor_window_default_geometry(station, orchestrator, qtbot):
    """With no saved geometry, MonitorWindow sizes itself to a non-zero fraction."""
    # The isolated_settings fixture already points the factory at an empty INI,
    # so no explicit key-clearing is needed here.
    win = MonitorWindow(station, orchestrator)
    qtbot.addWidget(win)
    assert win.width() > 0
    assert win.height() > 0


def test_closing_window_persists_to_isolated_ini_not_real_scope(
    station, orchestrator, qtbot, isolated_settings
):
    """Closing a window writes geometry to the throwaway INI, never the real registry.

    Pins the Phase 3 test seam: because app_settings.get_settings is monkeypatched
    to the tmp INI, closeEvent's setValue lands there. Seeing the key in that file
    is proof the real QSettings scope was left untouched by the test run.
    """
    win = MonitorWindow(station, orchestrator)
    qtbot.addWidget(win)
    win.show()
    win.close()

    settings = QSettings(str(isolated_settings), QSettings.Format.IniFormat)
    settings.sync()
    assert settings.value("MonitorWindow/geometry") is not None
    assert isolated_settings.exists()


# ── Phase 2: notification banner (replaces modal dialog storms) ────────────────

def test_banner_hidden_by_default(qtbot):
    """A fresh NotificationBanner is hidden until a message arrives."""
    banner = NotificationBanner()
    qtbot.addWidget(banner)
    assert banner.isHidden()
    assert banner.count == 0


def test_banner_error_shows_with_severity(qtbot):
    """show_message with 'error' makes the banner visible and sets the property."""
    banner = NotificationBanner()
    qtbot.addWidget(banner)
    banner.show_message("Magnet quench detected", "error")
    assert banner.isVisible()
    assert banner.property("severity") == "error"
    assert "Magnet quench detected" in banner._label.text()


def test_banner_warning_shows_with_severity(qtbot):
    """show_message with 'warning' sets the warning severity property."""
    banner = NotificationBanner()
    qtbot.addWidget(banner)
    banner.show_message("Action blocked while busy", "warning")
    assert banner.isVisible()
    assert banner.property("severity") == "warning"


def test_banner_dismiss_hides(qtbot):
    """Dismissing the banner hides it and resets the counter."""
    banner = NotificationBanner()
    qtbot.addWidget(banner)
    banner.show_message("Something happened", "warning")
    assert banner.isVisible()
    banner.dismiss()
    assert not banner.isVisible()
    assert banner.count == 0


def test_banner_repeat_increments_counter_no_stack(qtbot):
    """A repeated identical message bumps the counter instead of stacking."""
    banner = NotificationBanner()
    qtbot.addWidget(banner)
    banner.show_message("Blocked: magnet_x busy", "warning")
    assert banner.count == 1
    banner.show_message("Blocked: magnet_x busy", "warning")
    banner.show_message("Blocked: magnet_x busy", "warning")
    assert banner.count == 3
    assert "(3×)" in banner._label.text()
    # Still exactly one banner, still visible (nothing stacked).
    assert banner.isVisible()


def test_monitor_error_signal_drives_banner(monitor_win, orchestrator):
    """error_occurred routes to the MonitorWindow banner (no modal dialog)."""
    orchestrator.error_occurred.emit("Interlock tripped")
    assert monitor_win._banner.isVisible()
    assert monitor_win._banner.property("severity") == "error"
    assert "Interlock tripped" in monitor_win._banner._label.text()


def test_monitor_action_blocked_drives_banner(monitor_win, orchestrator):
    """action_blocked routes to the MonitorWindow banner as a warning."""
    orchestrator.action_blocked.emit("Cannot initiate: procedure running")
    assert monitor_win._banner.isVisible()
    assert monitor_win._banner.property("severity") == "warning"


def test_procedure_error_signal_drives_banner(procedure_win, orchestrator):
    """error_occurred routes to the ProcedureWindow banner as an error."""
    orchestrator.error_occurred.emit("Sweep failed")
    assert procedure_win._banner.isVisible()
    assert procedure_win._banner.property("severity") == "error"


def test_instrument_panel_has_no_action_blocked_handler(station, orchestrator, qtbot):
    """The per-panel modal warning handler was removed (banner replaces it)."""
    vi_name = "magnet_x"
    vi = station._virtual_instruments[vi_name]
    panel = InstrumentPanel(vi_name, vi, orchestrator)
    qtbot.addWidget(panel)
    assert not hasattr(panel, "_on_action_blocked")


# ── Phase 2: state-aware status bar ────────────────────────────────────────────

def test_status_bar_level_flips_on_state(monitor_win, orchestrator):
    """Status bar 'level' property tracks the Orchestrator state category."""
    # Active state → "active"
    orchestrator.state_changed.emit(OrchestratorState.RAMPING.value)
    assert monitor_win._status_bar.property("level") == "active"

    # Emergency → "error"
    orchestrator.state_changed.emit(OrchestratorState.EMERGENCY.value)
    assert monitor_win._status_bar.property("level") == "error"

    # Back to idle → default (empty)
    orchestrator.state_changed.emit(OrchestratorState.IDLE.value)
    assert monitor_win._status_bar.property("level") == ""


# ── Phase 2: light theme smoke check ───────────────────────────────────────────

def test_stylesheet_has_no_dark_theme_hexes():
    """build_stylesheet() no longer contains the old dark-theme background hexes."""
    qss = build_stylesheet().lower()
    for dark_hex in ("#121212", "#252526", "#1e1e1e", "#2d2d30"):
        assert dark_hex not in qss, f"Leftover dark-theme colour {dark_hex} in stylesheet"


# ── Phase 2: effective colours after descendant repolish (regression) ──────────
# Property-only assertions would not have caught the bug these tests pin down:
# repolishing only the parent left child QLabels with their stale colour, so
# these assert the EFFECTIVE palette colour under the real stylesheet.

@pytest.fixture
def themed_app(qapp):
    """Apply the real application stylesheet for the test, then restore it.

    The plain test QApplication has no stylesheet, so palette-based colour
    assertions only mean anything with build_stylesheet() applied.
    """
    qapp.setStyleSheet(build_stylesheet())
    yield qapp
    qapp.setStyleSheet("")


def test_banner_error_effective_label_color(themed_app, qtbot):
    """After an error show_message, the label's palette colour is the error text."""
    banner = NotificationBanner()
    qtbot.addWidget(banner)
    banner.show_message("Interlock tripped", "error")
    assert banner._label.palette().windowText().color().name() == BANNER_ERROR_TEXT


def test_banner_warning_effective_label_color(themed_app, qtbot):
    """Switching a visible banner to warning re-colours the label (child repolish)."""
    banner = NotificationBanner()
    qtbot.addWidget(banner)
    banner.show_message("Boom", "error")
    banner.show_message("Blocked", "warning")
    assert banner._label.palette().windowText().color().name() == BANNER_WARNING_TEXT


def test_status_bar_label_effective_color_flips(themed_app, station, orchestrator, qtbot):
    """The status-bar label renders white in EMERGENCY and dark again on IDLE."""
    win = MonitorWindow(station, orchestrator)
    qtbot.addWidget(win)
    win.show()

    orchestrator.state_changed.emit(OrchestratorState.EMERGENCY.value)
    assert win._state_label.palette().windowText().color().name() == TEXT_ON_ACCENT

    orchestrator.state_changed.emit(OrchestratorState.IDLE.value)
    assert win._state_label.palette().windowText().color().name() == TEXT_PRIMARY


# ── Session persistence tests ──────────────────────────────────────────────────
# The autouse isolated_settings fixture redirects both QSettings and the JSON
# session file into tmp_path, so these never touch the user's real AppData.


def _sample_stub():
    return lambda: {"sample_name": "s", "sample_id": "id", "comments": ""}


def _data_dir_stub():
    return lambda: "C:/CryoData"


def test_monitor_window_has_user_menu(monitor_win):
    """The menu bar has a leftmost 'User' menu (Setup tier: login, form autosave)."""
    titles = [a.text() for a in monitor_win.menuBar().actions()]
    assert "User" in titles
    assert titles[0] == "User"


def test_monitor_restores_sample_fields_from_session(station, orchestrator, qtbot, tmp_path):
    """Sample Info fields are populated from a saved session on open."""
    session_store.save(
        session_store.SessionState(
            sample_name="Si_001", sample_id="S2024-01",
            comments="cooldown 2", data_dir="D:/runs",
        ),
        tmp_path / "last_session.json",
    )
    win = MonitorWindow(station, orchestrator)
    qtbot.addWidget(win)
    assert win._session_info._sample_name_input.text() == "Si_001"
    assert win._session_info._sample_id_input.text() == "S2024-01"
    assert win._session_info._comments_input.toPlainText() == "cooldown 2"
    assert win._session_info._data_dir_input.text() == "D:/runs"


def test_monitor_saves_session_on_close(monitor_win, tmp_path):
    """Closing the window persists the current Sample Info to the session file."""
    monitor_win._session_info._sample_name_input.setText("SampleZ")
    monitor_win._session_info._data_dir_input.setText("E:/data")
    monitor_win.close()
    loaded = session_store.load(tmp_path / "last_session.json")
    assert loaded.sample_name == "SampleZ"
    assert loaded.data_dir == "E:/data"


def test_new_session_clears_fields(monitor_win, monkeypatch):
    """New Session (confirmed) resets the Sample Info fields to defaults."""
    monitor_win._session_info._sample_name_input.setText("ToClear")
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes
    )
    monitor_win._on_new_session()
    assert monitor_win._session_info._sample_name_input.text() == ""
    assert monitor_win._session_info._data_dir_input.text() == "C:/CryoData"


def test_procedure_window_restores_selection_and_params(station, orchestrator, qtbot):
    """A ProcedureWindow built with a session restores its selection and params."""
    info, ddir = _sample_stub(), _data_dir_stub()
    win = ProcedureWindow(station, orchestrator, info, ddir)
    qtbot.addWidget(win)
    proc_name = win._params_panel._current_procedure_name
    # Pick a plain text field (a QLineEdit) to type into.
    param_key = next(
        name for name, w in win._params_panel._param_inputs.items() if isinstance(w, QLineEdit)
    )
    win._params_panel._param_inputs[param_key].setText("42")

    state = session_store.SessionState()
    win.export_session_state(state)
    assert state.selected_procedure == proc_name
    # The cache is keyed by "{group.key}::{param}", so the typed value lands
    # under one composite key — assert it round-trips regardless of the prefix.
    assert "42" in state.procedure_params[proc_name].values()

    win2 = ProcedureWindow(station, orchestrator, info, ddir, initial_session=state)
    qtbot.addWidget(win2)
    assert win2._params_panel._proc_selector.currentText() == proc_name
    assert win2._params_panel._param_inputs[param_key].text() == "42"


def test_procedure_window_exports_and_restores_queue(station, orchestrator, qtbot):
    """A queued procedure round-trips through a session and is re-armed on restore."""
    info, ddir = _sample_stub(), _data_dir_stub()
    win = ProcedureWindow(station, orchestrator, info, ddir)
    qtbot.addWidget(win)
    win._on_add_to_queue()
    assert win._queue_panel._queue_list.count() == 1, "default form params should be valid to queue"

    state = session_store.SessionState()
    win.export_session_state(state)
    assert len(state.queue) == 1

    win2 = ProcedureWindow(station, orchestrator, info, ddir, initial_session=state)
    qtbot.addWidget(win2)
    assert win2._queue_panel._queue_list.count() == 1
    assert len(orchestrator._procedure_queue) == 1


def test_procedure_window_skips_unknown_procedure_in_queue(station, orchestrator, qtbot):
    """A saved queue item for an unknown procedure is skipped, not fatal."""
    info, ddir = _sample_stub(), _data_dir_stub()
    state = session_store.SessionState(
        queue=[session_store.QueueItemState(procedure="NoSuchProcedure")]
    )
    win = ProcedureWindow(station, orchestrator, info, ddir, initial_session=state)
    qtbot.addWidget(win)
    assert win._queue_panel._queue_list.count() == 0


def test_run_queue_marks_running_then_done(station, orchestrator, qtbot, monkeypatch):
    """Running the queue marks items running, then done as each finishes."""
    info, ddir = _sample_stub(), _data_dir_stub()
    win = ProcedureWindow(station, orchestrator, info, ddir)
    qtbot.addWidget(win)
    win._on_add_to_queue()
    win._on_add_to_queue()
    assert [e.status for e in win._queue_panel._queue] == ["pending", "pending"]

    # Stub the actual run: exercise only the GUI's per-item status logic.
    monkeypatch.setattr(orchestrator, "run_queue", lambda: None)
    win._queue_panel._on_run_queue()
    assert win._queue_panel._queue[0].status == "running"
    assert win._queue_panel._queue_running is True

    orchestrator.procedure_finished.emit()
    assert win._queue_panel._queue[0].status == "done"
    assert win._queue_panel._queue[1].status == "running"

    orchestrator.procedure_finished.emit()
    assert win._queue_panel._queue[1].status == "done"
    assert win._queue_panel._queue_running is False


def test_abort_marks_running_item_failed(station, orchestrator, qtbot, monkeypatch):
    """Aborting a queued run marks that item failed and promotes the next."""
    info, ddir = _sample_stub(), _data_dir_stub()
    win = ProcedureWindow(station, orchestrator, info, ddir)
    qtbot.addWidget(win)
    win._on_add_to_queue()
    win._on_add_to_queue()
    monkeypatch.setattr(orchestrator, "run_queue", lambda: None)
    monkeypatch.setattr(orchestrator, "abort_procedure", lambda: None)
    win._queue_panel._on_run_queue()
    assert win._queue_panel._queue[0].status == "running"

    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes
    )
    win._on_abort()
    assert win._queue_panel._queue[0].status == "failed"
    assert win._queue_panel._queue[1].status == "running"


def test_queue_remove_resyncs_orchestrator(station, orchestrator, qtbot):
    """Removing a pending queue item keeps the Orchestrator queue in sync."""
    info, ddir = _sample_stub(), _data_dir_stub()
    win = ProcedureWindow(station, orchestrator, info, ddir)
    qtbot.addWidget(win)
    win._on_add_to_queue()
    win._on_add_to_queue()
    assert len(orchestrator._procedure_queue) == 2
    win._queue_panel._queue_list.setCurrentRow(0)
    win._queue_panel._queue_remove()
    assert win._queue_panel._queue_list.count() == 1
    assert len(orchestrator._procedure_queue) == 1


# ── Config management + geometry tests ─────────────────────────────────────────

def _catalog(tmp_path):
    return ConfigCatalog(_app_settings.shipped_config_dir(), tmp_path / "user")


def test_monitor_no_config_menu_without_catalog(monitor_win):
    """Without a catalog, no Config menu appears (backward compatible)."""
    titles = [a.text() for a in monitor_win.menuBar().actions()]
    assert "Config" not in titles


def test_monitor_has_config_menu_with_catalog(station, orchestrator, qtbot, tmp_path):
    """A catalog wires in a Config menu listing the shipped configs."""
    win = MonitorWindow(station, orchestrator, catalog=_catalog(tmp_path))
    qtbot.addWidget(win)
    titles = [a.text() for a in win.menuBar().actions()]
    assert "Config" in titles


def test_select_config_confirmed_triggers_restart(station, orchestrator, qtbot, tmp_path, monkeypatch):
    """Confirming a config switch persists it and calls the restart callback."""
    restarted = []
    catalog = _catalog(tmp_path)
    win = MonitorWindow(
        station, orchestrator, catalog=catalog,
        active_config_path="/nowhere/active",
        restart_callback=lambda: restarted.append(True),
    )
    qtbot.addWidget(win)
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes
    )
    entry = catalog.list_configs()[0]
    win._config_controller._on_select_config(str(entry.path))
    assert restarted == [True]
    assert _app_settings.config_active() == (entry.name, entry.source)


def test_select_config_cancelled_does_not_restart(station, orchestrator, qtbot, tmp_path, monkeypatch):
    """Declining the switch warning does not restart."""
    restarted = []
    catalog = _catalog(tmp_path)
    win = MonitorWindow(
        station, orchestrator, catalog=catalog,
        active_config_path="/nowhere/active",
        restart_callback=lambda: restarted.append(True),
    )
    qtbot.addWidget(win)
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No
    )
    win._config_controller._on_select_config(str(catalog.list_configs()[0].path))
    assert restarted == []


def test_startup_candidates_end_with_sim_and_dedup(monkeypatch):
    """The candidate chain always ends with sim_cryostat and has no duplicates."""
    from cryosoft import main as app_main

    monkeypatch.setattr(_app_settings, "config_active", lambda: None)
    candidates = app_main._startup_candidates()
    assert Path(candidates[-1]).name == "sim_cryostat"
    assert len(candidates) == len(set(candidates))


def test_startup_candidates_inserts_shipped_baseline_for_user_config(tmp_path, monkeypatch):
    """An active user config is followed by its shipped baseline, then sim."""
    from cryosoft import main as app_main

    catalog = _catalog(tmp_path)
    entry = catalog.fork_shipped("sim_cryostat", "sim_cryostat")
    monkeypatch.setattr(_app_settings, "user_config_dir", lambda: tmp_path / "user")
    monkeypatch.setattr(
        _app_settings, "config_active", lambda: (entry.name, entry.source)
    )
    candidates = app_main._startup_candidates()
    assert candidates[0] == str(entry.path)
    shipped_sim = str(_app_settings.shipped_config_dir() / "sim_cryostat")
    assert shipped_sim in candidates


def test_startup_warning_shown_in_banner(station, orchestrator, qtbot, tmp_path):
    """A startup fallback warning is surfaced in the notification banner."""
    win = MonitorWindow(
        station, orchestrator, catalog=_catalog(tmp_path),
        startup_warning="active config was invalid",
    )
    qtbot.addWidget(win)
    assert not win._banner.isHidden()


def test_offscreen_saved_geometry_recenters(station, orchestrator, qtbot):
    """A saved geometry that lands off-screen is discarded for a centered one."""
    win = MonitorWindow(station, orchestrator)
    qtbot.addWidget(win)
    win.move(-10000, -10000)
    assert not window_geometry.geometry_on_screen(win)
    _app_settings.get_settings().setValue("MonitorWindow/geometry", win.saveGeometry())

    win2 = MonitorWindow(station, orchestrator)
    qtbot.addWidget(win2)
    assert window_geometry.geometry_on_screen(win2)


# ── Monitoring toggle (Orchestrator start/stop monitoring from the header) ────


def test_monitoring_button_starts_and_stops_monitoring(monitor_win, orchestrator):
    """The header toggle starts monitoring, then stops it again in IDLE."""
    btn = monitor_win.findChild(QPushButton, "monitoring_btn")
    assert btn is not None
    # Launch state: monitoring off, button offers to start it.
    assert orchestrator.is_monitoring() is False
    assert not btn.isChecked()
    assert btn.text() == "Start Monitoring"

    btn.click()
    assert orchestrator.is_monitoring() is True
    assert btn.isChecked()
    assert btn.text() == "Stop Monitoring"

    btn.click()  # IDLE, so the stop is allowed
    assert orchestrator.is_monitoring() is False
    assert btn.text() == "Start Monitoring"


def test_monitoring_button_mirrors_orchestrator_state(monitor_win, orchestrator):
    """Starting monitoring on the Orchestrator directly updates the toggle."""
    btn = monitor_win.findChild(QPushButton, "monitoring_btn")
    orchestrator.start_monitoring()
    assert btn.isChecked()
    assert btn.text() == "Stop Monitoring"


def test_monitoring_button_snaps_back_when_stop_refused(monitor_win, orchestrator):
    """A refused stop (non-IDLE state) re-syncs the button and warns via banner."""
    btn = monitor_win.findChild(QPushButton, "monitoring_btn")
    orchestrator.start_monitoring()
    # Force a non-IDLE state so stop_monitoring() is refused.
    orchestrator._state = OrchestratorState.RAMPING

    btn.click()  # attempt to stop

    assert orchestrator.is_monitoring() is True
    assert btn.isChecked(), "button must snap back to the confirmed state"
    assert monitor_win._banner.isVisible()
    orchestrator._state = OrchestratorState.IDLE


# ── DiagnosticsWindow tests ─────────────────────────────────────────────────────────

_STALLED_RECORD = {
    "orch_state": "RAMPING",
    "elapsed_in_state_s": 12.3,
    "verdict": "RAMP_STALLED",
    "alerts": ["magnet: ramp stalled (gap 0.5 not closing for 6 ticks)"],
    "vis": [
        {
            "vi_name": "magnet", "value": 1.0, "target": 1.5, "gap": 0.5,
            "closing": 0.0, "rate": 0.1, "eta_s": 300.0,
            "ramp_status": "RAMPING", "phase": None,
            "code": "RAMP_STALLED", "detail": "gap 0.5 not closing for 6 ticks",
        },
        {
            "vi_name": "temp_ctrl", "value": 4.2, "target": 4.2, "gap": 0.0,
            "closing": 0.0, "rate": 0.0, "eta_s": None,
            "ramp_status": "TARGET_REACHED", "phase": None,
            "code": "OK", "detail": "",
        },
    ],
}


def test_monitor_window_has_diagnostics_menu(monitor_win):
    """The menu bar exposes a Diagnostics menu alongside Session/Procedures."""
    menu_titles = {action.text() for action in monitor_win.menuBar().actions()}
    assert "Diagnostics" in menu_titles


def test_monitor_opens_diagnostics_window_from_menu(monitor_win):
    """Triggering the Diagnostics menu's action lazily creates and shows DiagnosticsWindow."""
    assert monitor_win._diagnostics_window is None
    monitor_win._open_diagnostics_window()
    assert monitor_win._diagnostics_window is not None
    assert monitor_win._diagnostics_window.isVisible()


def test_diagnostics_window_opens(diagnostics_win):
    """DiagnosticsWindow constructs and is visible."""
    assert diagnostics_win.isVisible()


def test_diagnostics_window_placeholder_before_first_tick(diagnostics_win):
    """Before any operational_status tick, the window shows a neutral placeholder."""
    assert "No live data yet" in diagnostics_win._state_label.text()
    assert diagnostics_win._table.rowCount() == 0
    assert diagnostics_win._alerts_view.toPlainText() == "No active alerts."


def test_diagnostics_window_renders_operational_status(diagnostics_win, orchestrator):
    """A live operational_status tick populates state, verdict, table, and alerts."""
    orchestrator.operational_status.emit(_STALLED_RECORD)

    assert "RAMPING" in diagnostics_win._state_label.text()
    assert diagnostics_win._verdict_badge.text() == "RAMP_STALLED"
    assert diagnostics_win._verdict_badge.property("severity") == "warning"

    assert diagnostics_win._table.rowCount() == 2
    assert diagnostics_win._table.item(0, 0).text() == "magnet"
    assert diagnostics_win._table.item(0, 1).text() == "RAMP_STALLED"
    assert "gap 0.5" in diagnostics_win._table.item(0, 2).text()
    assert diagnostics_win._table.item(1, 0).text() == "temp_ctrl"
    assert diagnostics_win._table.item(1, 1).text() == "OK"

    assert "ramp stalled" in diagnostics_win._alerts_view.toPlainText()


def test_diagnostics_window_verdict_badge_error_severity(diagnostics_win, orchestrator):
    """A VI_DISCONNECTED verdict drives the badge to 'error' severity."""
    record = dict(_STALLED_RECORD, verdict="VI_DISCONNECTED")
    orchestrator.operational_status.emit(record)
    assert diagnostics_win._verdict_badge.property("severity") == "error"


def test_diagnostics_window_ok_verdict_clears_alerts_placeholder(diagnostics_win, orchestrator):
    """An OK tick with no alerts restores the 'no active alerts' placeholder."""
    ok_record = {
        "orch_state": "IDLE", "elapsed_in_state_s": 1.0, "verdict": "OK",
        "alerts": [], "vis": [],
    }
    orchestrator.operational_status.emit(_STALLED_RECORD)
    orchestrator.operational_status.emit(ok_record)
    assert diagnostics_win._verdict_badge.property("severity") == "ok"
    assert diagnostics_win._alerts_view.toPlainText() == "No active alerts."
    assert diagnostics_win._table.rowCount() == 0


def test_diagnostics_window_seeds_from_existing_status(orchestrator, qtbot):
    """A DiagnosticsWindow opened after ticks already happened seeds from get_operational_status()."""
    # A raw signal emit does not update Orchestrator's stored record — only a
    # real tick does that (_update_operational_status). Setting the private
    # field directly is the same forcing pattern other GUI tests use for
    # Orchestrator internals (e.g. `orchestrator._state = ...`).
    orchestrator._operational_status = _STALLED_RECORD
    win = DiagnosticsWindow(orchestrator)
    qtbot.addWidget(win)
    win.show()
    assert win._verdict_badge.text() == "RAMP_STALLED"


def test_diagnostics_window_copy_diagnostics_to_clipboard(diagnostics_win, orchestrator, qtbot):
    """Copy Diagnostics puts a plain-text summary on the clipboard."""
    orchestrator.operational_status.emit(_STALLED_RECORD)
    copy_btn = diagnostics_win.findChild(QPushButton, "copy_diagnostics_btn")
    assert copy_btn is not None
    copy_btn.click()

    clipboard_text = QApplication.clipboard().text()
    assert "RAMP_STALLED" in clipboard_text
    assert "magnet" in clipboard_text
    assert "ramp stalled" in clipboard_text
    assert diagnostics_win._status_bar.currentMessage() == "Diagnostics copied to clipboard"


def test_diagnostics_window_copy_diagnostics_before_any_tick(diagnostics_win):
    """Copy Diagnostics before any tick copies a clear placeholder, not a crash."""
    copy_btn = diagnostics_win.findChild(QPushButton, "copy_diagnostics_btn")
    copy_btn.click()
    assert "No live status available" in QApplication.clipboard().text()


def test_diagnostics_window_geometry_persists_on_close(orchestrator, qtbot, isolated_settings):
    """DiagnosticsWindow geometry is saved under its own QSettings key on close."""
    from PyQt6.QtCore import QSettings

    win = DiagnosticsWindow(orchestrator)
    qtbot.addWidget(win)
    win.show()
    win.close()

    settings = QSettings(str(isolated_settings), QSettings.Format.IniFormat)
    assert settings.value("DiagnosticsWindow/geometry") is not None
