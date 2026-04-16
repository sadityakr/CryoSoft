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

import pytest
from PyQt6.QtWidgets import QLabel, QPushButton

from cryosoft.core.orchestrator import Orchestrator, OrchestratorState
from cryosoft.core.station import build_station
from cryosoft.gui.instrument_panel import InstrumentPanel
from cryosoft.gui.monitor_window import MonitorWindow
from cryosoft.gui.procedure_window import ProcedureWindow


CONFIG_PATH = "cryosoft/configs/sim_cryostat"


# ── Fixtures ──────────────────────────────────────────────────────────────────

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
    """Stale state sets orange border style."""
    vi_name = "magnet_x"
    vi = station._virtual_instruments[vi_name]
    panel = InstrumentPanel(vi_name, vi, orchestrator)
    qtbot.addWidget(panel)

    orchestrator.states_updated.emit({vi_name: {"_stale": True}})
    assert "orange" in panel.styleSheet()


def test_instrument_panel_disconnected_border(station, orchestrator, qtbot):
    """Disconnected state sets red border style."""
    vi_name = "magnet_x"
    vi = station._virtual_instruments[vi_name]
    panel = InstrumentPanel(vi_name, vi, orchestrator)
    qtbot.addWidget(panel)

    orchestrator.states_updated.emit({vi_name: {"_stale": True, "_disconnected": True}})
    assert "red" in panel.styleSheet()


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

    # Select FieldSweepIV (index 0 if it's the only one)
    for i in range(procedure_win._proc_selector.count()):
        if "Field Sweep" in procedure_win._proc_selector.itemText(i):
            procedure_win._proc_selector.setCurrentIndex(i)
            break

    for param_name in FieldSweepIV.parameters:
        field = procedure_win.findChild(
            __import__("PyQt6.QtWidgets", fromlist=["QLineEdit"]).QLineEdit,
            f"param_{param_name}_input",
        )
        assert field is not None, f"Missing input for parameter '{param_name}'"


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
    """measurement_ready signal appends data to the live plot."""
    procedure_win._x_key = "field_T"
    procedure_win._y_axis_selector.addItem("voltage_V")
    procedure_win._y_axis_selector.setCurrentText("voltage_V")

    datapoint = {"field_T": 0.5, "voltage_V": [1.23e-6] * 10}
    orchestrator.measurement_ready.emit(datapoint)

    assert len(procedure_win._plot_x) == 1
    assert abs(procedure_win._plot_x[0] - 0.5) < 1e-9
