# ---
# description: |
#   Smoke tests for the CryoSoft GUI layer (Layer 6).
#   Verifies that MonitorWindow and ProcedureWindow open without errors,
#   that InstrumentPanel widgets are auto-generated for all registered VIs,
#   and that Orchestrator signals (state_changed, procedure_progress,
#   measurement_ready) update the GUI correctly.
# last_updated: 2026-04-16
# ---

"""GUI smoke tests — Layer 6.

These tests use pytest-qt (qtbot fixture). They run against the sim_cryostat
config with no hardware. All 121 prior tests must pass before this file is run.
"""

import logging

import pytest
from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import (
    QComboBox,
    QDockWidget,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSplitter,
)

from cryosoft.core.orchestrator import Orchestrator, OrchestratorState
from cryosoft.core.station import build_station
from cryosoft.gui.instrument_panel import InstrumentPanel
from cryosoft.gui.monitor_window import MonitorWindow
from cryosoft.gui.notification_banner import NotificationBanner
from cryosoft.gui import session as session_store
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
    monkeypatch.setattr(app_settings, "session_file_path", lambda: session_path)
    return ini_path


@pytest.fixture
def station():
    """Real simulated station from sim_cryostat config."""
    return build_station(CONFIG_PATH)


@pytest.fixture
def orchestrator(station, qtbot):
    """Orchestrator with a short tick for fast tests."""
    return Orchestrator(station, tick_interval_ms=50)


@pytest.fixture
def monitor_win(station, orchestrator, qtbot):
    """MonitorWindow shown via qtbot."""
    win = MonitorWindow(station, orchestrator)
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
    """InstrumentPanel has Initiate and Standby buttons."""
    vi_name = "temperature_vti"
    vi = station._virtual_instruments[vi_name]
    panel = InstrumentPanel(vi_name, vi, orchestrator)
    qtbot.addWidget(panel)

    assert panel.findChild(QPushButton, f"{vi_name}_initiate_btn") is not None
    assert panel.findChild(QPushButton, f"{vi_name}_standby_btn") is not None


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
    assert "[stale]" in panel.title()


def test_instrument_panel_disconnected_border(station, orchestrator, qtbot):
    """Disconnected state sets the 'disconnected' status property (red border via QSS)."""
    vi_name = "magnet_x"
    vi = station._virtual_instruments[vi_name]
    panel = InstrumentPanel(vi_name, vi, orchestrator)
    qtbot.addWidget(panel)

    orchestrator.states_updated.emit({vi_name: {"_stale": True, "_disconnected": True}})
    assert panel.property("status") == "disconnected"
    assert "[DISCONNECTED]" in panel.title()


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
    assert panel.title() == vi_name


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
    """Procedure selector has at least one entry (FieldSweepIV)."""
    assert procedure_win._proc_selector.count() >= 1


def test_procedure_param_inputs_exist(procedure_win):
    """Parameter form inputs are created for the selected procedure."""
    from cryosoft.procedures.field_sweep_iv import FieldSweepIV

    # Select FieldSweepIV by its exact name. A substring match ("Field Sweep")
    # is ambiguous with Field Sweep DC, and procedure discovery order depends
    # on import order across the test session — this made the test flaky.
    for i in range(procedure_win._proc_selector.count()):
        if procedure_win._proc_selector.itemText(i) == FieldSweepIV.name:
            procedure_win._proc_selector.setCurrentIndex(i)
            break
    else:
        pytest.fail("FieldSweepIV not found in procedure selector")

    for param_name in FieldSweepIV.parameters:
        field = procedure_win.findChild(
            __import__("PyQt6.QtWidgets", fromlist=["QLineEdit"]).QLineEdit,
            f"param_{param_name}_input",
        )
        assert field is not None, f"Missing input for parameter '{param_name}'"


def test_procedure_param_label_and_tooltip(procedure_win):
    """Param label is the canonical `name (unit):` and carries the description tooltip.

    The label is the same key stored under /metadata/procedure_params in the
    HDF5 output (see BaseProcedure), not prose. The prose description lives in
    a tooltip on both the input field and its form label.
    """
    from cryosoft.procedures.field_sweep_iv import FieldSweepIV

    for i in range(procedure_win._proc_selector.count()):
        if procedure_win._proc_selector.itemText(i) == FieldSweepIV.name:
            procedure_win._proc_selector.setCurrentIndex(i)
            break
    else:
        pytest.fail("FieldSweepIV not found in procedure selector")

    spec = FieldSweepIV.sweep_parameters["field_start"]
    field = procedure_win.findChild(QLineEdit, "param_field_start_input")
    assert field is not None, "Missing input for parameter 'field_start'"

    assert field.text() == str(spec["default"])

    form = field.parent().layout()
    assert isinstance(form, QFormLayout)
    row_label = form.labelForField(field)
    assert isinstance(row_label, QLabel)
    assert row_label.text() == "field_start (T):"

    for tooltip in (field.toolTip(), row_label.toolTip()):
        assert tooltip, "Tooltip must be non-empty"
        assert spec["description"] in tooltip


def test_monitor_sample_info_inputs_exist(monitor_win):
    """Sample name, ID, and comments fields are present in MonitorWindow."""
    assert monitor_win._sample_name_input is not None
    assert monitor_win._sample_id_input is not None
    assert monitor_win._comments_input is not None


def test_monitor_data_dir_input_exists(monitor_win):
    """Data directory input is in MonitorWindow and has a default value."""
    assert monitor_win._data_dir_input is not None
    assert monitor_win._data_dir_input.text() != ""


def test_monitor_get_sample_info(monitor_win):
    """get_sample_info() returns a dict with the correct keys."""
    info = monitor_win.get_sample_info()
    assert "sample_name" in info
    assert "sample_id" in info
    assert "comments" in info


def test_monitor_get_data_dir(monitor_win):
    """get_data_dir() returns a non-empty string."""
    assert monitor_win.get_data_dir() != ""


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
    initial_count = procedure_win._queue_list.count()
    qtbot.mouseClick(
        procedure_win.findChild(QPushButton, "add_to_queue_btn"),
        __import__("PyQt6.QtCore", fromlist=["Qt"]).Qt.MouseButton.LeftButton,
    )
    assert procedure_win._queue_list.count() == initial_count + 1


def test_measurement_ready_updates_plot(procedure_win, orchestrator):
    """measurement_ready signal appends the datapoint to _datapoints."""
    datapoint = {"field_T": 0.5, "voltage_V": [1.23e-6] * 10}
    orchestrator.measurement_ready.emit(datapoint)

    assert len(procedure_win._datapoints) == 1
    assert abs(procedure_win._datapoints[0]["field_T"] - 0.5) < 1e-9


# ── Dock-based layout tests (Monitor GUI redesign) ──────────────────────────

def test_monitor_dock_host_exists(monitor_win):
    """The dock host is an inner QMainWindow with dock nesting enabled."""
    dock_host = monitor_win.findChild(QMainWindow, "dock_host")
    assert dock_host is not None
    assert dock_host is monitor_win._dock_host
    assert dock_host.isDockNestingEnabled()


def test_monitor_instrument_docks_exist_for_system_vis(monitor_win, station):
    """One dock_{vi_name} QDockWidget exists per system/level VI, wrapping its InstrumentPanel."""
    system_vis = [
        n for n in station.get_vi_names() if station.get_vi_type(n) in {"system", "level"}
    ]
    assert system_vis, "sim_cryostat should have at least one system/level VI"
    for vi_name in system_vis:
        dock = monitor_win.findChild(QDockWidget, f"dock_{vi_name}")
        assert dock is not None, f"Missing dock for {vi_name}"
        assert isinstance(dock.widget(), InstrumentPanel)
        assert dock.widget()._vi_name == vi_name


def test_monitor_fixed_docks_exist_with_expected_content(monitor_win):
    """Other Devices, Log, and Sample Info each have a dock wrapping the expected widget."""
    other_dock = monitor_win.findChild(QDockWidget, "dock_other_devices")
    log_dock = monitor_win.findChild(QDockWidget, "dock_log")
    sample_dock = monitor_win.findChild(QDockWidget, "dock_sample_info")
    assert other_dock is not None
    assert log_dock is not None
    assert sample_dock is not None
    assert log_dock.widget() is monitor_win._log_widget
    assert sample_dock.widget() is not None


def test_monitor_default_trend_docks_exist(monitor_win):
    """Two trend docks exist by default, each wrapping its registered TrendPlotPanel."""
    assert len(monitor_win._trend_panels) == 2
    for panel_id, panel in monitor_win._trend_panels.items():
        dock = monitor_win.findChild(QDockWidget, f"dock_{panel_id}")
        assert dock is not None
        assert dock.widget() is panel


def test_monitor_view_menu_has_toggle_action_per_dock(monitor_win, station):
    """The View menu lists a checkable toggle action for every dock."""
    system_vis = [
        n for n in station.get_vi_names() if station.get_vi_type(n) in {"system", "level"}
    ]
    action_texts = {a.text() for a in monitor_win._view_menu.actions() if a.text()}

    for vi_name in system_vis:
        assert vi_name in action_texts
    for expected in ("Other Devices", "Log", "Sample Info", "Trend — trend_0", "Trend — trend_1"):
        assert expected in action_texts

    dock_titles = {"Other Devices", "Log", "Sample Info", "Trend — trend_0", "Trend — trend_1", *system_vis}
    for action in monitor_win._view_menu.actions():
        if action.text() in dock_titles:
            assert action.isCheckable()


def test_monitor_view_menu_toggle_hides_and_shows_dock(monitor_win):
    """Triggering a dock's toggleViewAction hides, then shows, that dock again."""
    dock = monitor_win.findChild(QDockWidget, "dock_log")
    action = dock.toggleViewAction()
    assert action.isChecked() is True
    assert dock.isVisible()

    action.trigger()
    assert dock.isVisible() is False
    assert action.isChecked() is False

    action.trigger()
    assert dock.isVisible() is True
    assert action.isChecked() is True


def test_monitor_add_trend_plot_action_caps_at_four(monitor_win):
    """The View menu's 'Add trend plot' action adds panels up to 4, then disables and stays inert."""
    assert len(monitor_win._trend_panels) == 2
    assert monitor_win._add_trend_action.isEnabled()

    monitor_win._add_trend_action.trigger()
    assert len(monitor_win._trend_panels) == 3
    monitor_win._add_trend_action.trigger()
    assert len(monitor_win._trend_panels) == 4
    assert not monitor_win._add_trend_action.isEnabled()

    monitor_win._add_trend_action.trigger()
    assert len(monitor_win._trend_panels) == 4


def test_monitor_trend_remove_button_drops_panel_never_below_one(monitor_win):
    """The panel's own remove button destroys the panel+dock, stopping at a floor of 1."""
    assert len(monitor_win._trend_panels) == 2
    first_id = next(iter(monitor_win._trend_panels))
    first_dock_name = monitor_win._trend_docks[first_id].objectName()

    monitor_win._on_trend_remove_requested(first_id)
    assert len(monitor_win._trend_panels) == 1
    assert first_id not in monitor_win._trend_panels
    assert monitor_win.findChild(QDockWidget, first_dock_name) is None  # actually destroyed

    remaining_id = next(iter(monitor_win._trend_panels))
    monitor_win._on_trend_remove_requested(remaining_id)
    assert len(monitor_win._trend_panels) == 1  # floor holds

    assert monitor_win._add_trend_action.isEnabled()


def test_monitor_trend_dock_close_only_hides_not_removes(monitor_win):
    """A trend dock's own close button (Qt's default) hides it — it stays registered.

    This is the "collapse" behavior every dock gets for free from
    toggleViewAction(); genuine removal is only via the panel's own remove
    button (see test_monitor_trend_remove_button_drops_panel_never_below_one).
    """
    panel_id = next(iter(monitor_win._trend_panels))
    dock = monitor_win._trend_docks[panel_id]

    dock.close()
    assert dock.isVisible() is False
    assert panel_id in monitor_win._trend_panels
    assert panel_id in monitor_win._trend_docks

    dock.show()
    assert dock.isVisible() is True


def test_monitor_states_updated_feeds_history_and_trend_combos(monitor_win, orchestrator):
    """states_updated records into MonitorHistory and populates the trend Y combos."""
    fake_state = {"magnet_x": {"get_field": 0.25, "magnet_current": 12.0}}
    orchestrator.states_updated.emit(fake_state)

    assert "magnet_x_get_field" in monitor_win._history.keys()

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

    trend_0 = monitor_win._trend_panels["trend_0"]
    trend_1 = monitor_win._trend_panels["trend_1"]
    assert trend_0.selected_key().endswith("_temperature")
    assert trend_1.selected_key().endswith(("_helium_level", "_nitrogen_level"))


def test_monitor_persistence_roundtrip_dock_state_and_trends(
    station, orchestrator, qtbot, isolated_settings
):
    """'Save layout' persists dock_state + trend selections; a fresh window restores them.

    Mirrors the existing geometry-persistence test: build a window, change
    state, save+close it (writes to the isolated ini), then build a fresh
    window against the same settings and check the state came back.
    """
    win1 = MonitorWindow(station, orchestrator)
    qtbot.addWidget(win1)
    win1.show()

    third_id = win1._add_trend_panel()
    third_panel = win1._trend_panels[third_id]

    # Feed history AFTER the third panel exists so its refresh() (triggered by
    # this emit) populates its Y combo with a real key to select.
    fake_state = {"magnet_x": {"get_field": 0.5, "magnet_current": 10.0}}
    orchestrator.states_updated.emit(fake_state)
    third_panel.set_selected_key("magnet_x_get_field")
    third_panel.set_selected_window_s(21600.0)  # "6 h"

    assert len(win1._trend_panels) == 3

    win1._on_save_layout()
    assert win1._status_bar.currentMessage() == "Layout saved"

    win1.close()

    win2 = MonitorWindow(station, orchestrator)
    qtbot.addWidget(win2)
    win2.show()

    assert len(win2._trend_panels) == 3

    # Give the new window's (empty) history the same key so the persisted
    # selection, held pending, can actually be applied.
    orchestrator.states_updated.emit(fake_state)

    third_id_2 = list(win2._trend_panels.keys())[2]
    third_panel_2 = win2._trend_panels[third_id_2]
    assert third_panel_2.selected_key() == "magnet_x_get_field"
    assert third_panel_2.selected_window_s() == 21600.0


def test_monitor_default_layout_when_settings_empty(monitor_win):
    """With no saved dock_state (fresh isolated settings), the DEFAULT layout stands."""
    assert len(monitor_win._trend_panels) == 2
    assert monitor_win._instrument_docks  # instrument docks were built and placed
    assert monitor_win._default_dock_state is not None


def test_monitor_restore_default_layout_action_resets_trend_docks(monitor_win):
    """'Restore default layout' rebuilds exactly the default trend-dock count, with a status message."""
    monitor_win._add_trend_action.trigger()
    monitor_win._add_trend_action.trigger()
    assert len(monitor_win._trend_panels) == 4

    monitor_win._on_restore_default_layout()
    assert len(monitor_win._trend_panels) == 2
    assert monitor_win._status_bar.currentMessage() == "Default layout restored"


def test_procedure_splitters_not_collapsible(procedure_win):
    """Both ProcedureWindow splitters have children-collapsing disabled."""
    splitters = procedure_win.findChildren(QSplitter)
    assert len(splitters) == 2, f"Expected 2 splitters, found {len(splitters)}"
    for sp in splitters:
        assert sp.childrenCollapsible() is False


def test_monitor_central_widget_not_scroll_area(monitor_win):
    """The central widget is the content widget directly; the dock host is a plain QMainWindow."""
    assert not isinstance(monitor_win.centralWidget(), QScrollArea)
    assert isinstance(monitor_win._dock_host, QMainWindow)


def test_log_handler_removed_on_close(station, orchestrator, qtbot):
    """Closing MonitorWindow detaches its log handler from the cryosoft logger."""
    win = MonitorWindow(station, orchestrator)
    qtbot.addWidget(win)
    handler = win._log_handler
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


def test_monitor_window_has_session_menu(monitor_win):
    """The menu bar has a leftmost 'Session' menu."""
    titles = [a.text() for a in monitor_win.menuBar().actions()]
    assert "Session" in titles
    assert titles[0] == "Session"  # leftmost, by convention


def test_monitor_restores_sample_fields_from_session(station, orchestrator, qtbot, tmp_path):
    """Sample Info fields are populated from a saved session on open."""
    session_store.save(
        session_store.SessionState(
            sample_name="Si_001",
            sample_id="S2024-01",
            comments="cooldown 2",
            data_dir="D:/runs",
        ),
        tmp_path / "last_session.json",
    )
    win = MonitorWindow(station, orchestrator)
    qtbot.addWidget(win)
    assert win._sample_name_input.text() == "Si_001"
    assert win._sample_id_input.text() == "S2024-01"
    assert win._comments_input.toPlainText() == "cooldown 2"
    assert win._data_dir_input.text() == "D:/runs"


def test_monitor_saves_session_on_close(monitor_win, tmp_path):
    """Closing the window persists the current Sample Info to the session file."""
    monitor_win._sample_name_input.setText("SampleZ")
    monitor_win._data_dir_input.setText("E:/data")
    monitor_win.close()
    loaded = session_store.load(tmp_path / "last_session.json")
    assert loaded.sample_name == "SampleZ"
    assert loaded.data_dir == "E:/data"


def test_new_session_clears_fields(monitor_win, monkeypatch):
    """New Session (confirmed) resets the Sample Info fields to defaults."""
    from PyQt6.QtWidgets import QMessageBox

    monitor_win._sample_name_input.setText("ToClear")
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes
    )
    monitor_win._on_new_session()
    assert monitor_win._sample_name_input.text() == ""
    assert monitor_win._data_dir_input.text() == "C:/CryoData"


def test_new_session_cancel_keeps_fields(monitor_win, monkeypatch):
    """Declining the New Session confirmation leaves the fields untouched."""
    from PyQt6.QtWidgets import QMessageBox

    monitor_win._sample_name_input.setText("Keep")
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No
    )
    monitor_win._on_new_session()
    assert monitor_win._sample_name_input.text() == "Keep"


def test_procedure_window_restores_selection_and_params(station, orchestrator, qtbot):
    """A ProcedureWindow built with a session restores its selection and params."""
    info, ddir = _sample_stub(), _data_dir_stub()
    win = ProcedureWindow(station, orchestrator, info, ddir)
    qtbot.addWidget(win)

    proc_name = win._current_procedure_name
    param_key = next(iter(win._param_inputs))
    win._param_inputs[param_key].setText("42")

    state = session_store.SessionState()
    win.export_session_state(state)
    assert state.selected_procedure == proc_name
    assert state.procedure_params[proc_name][param_key] == "42"

    # Reuse the fixture Orchestrator (avoids a second live QTimer in teardown).
    win2 = ProcedureWindow(station, orchestrator, info, ddir, initial_session=state)
    qtbot.addWidget(win2)
    assert win2._proc_selector.currentText() == proc_name
    assert win2._param_inputs[param_key].text() == "42"


def test_procedure_window_exports_and_restores_queue(station, orchestrator, qtbot):
    """A queued procedure round-trips through a session and is re-armed on restore."""
    info, ddir = _sample_stub(), _data_dir_stub()
    win = ProcedureWindow(station, orchestrator, info, ddir)
    qtbot.addWidget(win)

    win._on_add_to_queue()
    assert win._queue_list.count() == 1, "default form params should be valid to queue"

    state = session_store.SessionState()
    win.export_session_state(state)
    assert len(state.queue) == 1

    # Restore into a second window on the SAME Orchestrator: _restore_queue
    # clears the pending queue and re-arms it, so the count stays 1.
    win2 = ProcedureWindow(station, orchestrator, info, ddir, initial_session=state)
    qtbot.addWidget(win2)
    assert win2._queue_list.count() == 1
    assert len(orchestrator._procedure_queue) == 1


def test_monitor_persists_procedure_queue_on_close(monitor_win, tmp_path):
    """Closing the Monitor persists an open ProcedureWindow's queue."""
    monitor_win._open_procedures()
    pw = monitor_win._procedure_window
    pw._on_add_to_queue()
    assert pw._queue_list.count() == 1
    monitor_win.close()
    loaded = session_store.load(tmp_path / "last_session.json")
    assert len(loaded.queue) == 1


def test_procedure_window_skips_unknown_procedure_in_queue(station, orchestrator, qtbot):
    """A saved queue item for an unknown procedure is skipped, not fatal."""
    info, ddir = _sample_stub(), _data_dir_stub()
    state = session_store.SessionState(
        queue=[session_store.QueueItemState(procedure="NoSuchProcedure")]
    )
    win = ProcedureWindow(station, orchestrator, info, ddir, initial_session=state)
    qtbot.addWidget(win)
    assert win._queue_list.count() == 0
